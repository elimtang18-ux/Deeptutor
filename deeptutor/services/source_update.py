"""
Source-checkout updater.

Pulls the latest upstream code for installations that run straight from a
Git checkout (``detect_installation()`` reports ``mode="source"``). The
managed PyPI updater in :mod:`deeptutor.services.app_update` deliberately
refuses those installs, so this module owns the equivalent flow:

1. back up uncommitted work (patch + untracked files),
2. ``git fetch`` + ``git merge --ff-only`` (never discards local commits),
3. reinstall Python/JS dependencies when their manifests changed,
4. restart the launchd-supervised launcher.

The heavy lifting runs in a detached worker process
(:mod:`deeptutor.runtime.source_update_worker`) so the API can answer the
request immediately and the worker survives the launcher restart it triggers.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Literal
import uuid

from deeptutor.services.app_update import LAUNCHER_PID_ENV, launcher_available
from deeptutor.services.file_io import atomic_write_json
from deeptutor.runtime.home import get_runtime_home
from deeptutor.runtime.process import is_process_alive

SourceUpdateJobStatus = Literal[
    "backup", "fetching", "merging", "installing", "restarting", "succeeded", "failed"
]


class SourceUpdateInProgressError(RuntimeError):
    """Another source update is already running."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def checkout_root() -> Path | None:
    """The Git checkout the imported package lives in, if any."""
    try:
        root = Path(__file__).resolve().parents[2]
    except IndexError:
        return None
    if (root / ".git").exists() and (root / "pyproject.toml").is_file():
        return root
    return None


def source_update_store_root(home: str | Path | None = None) -> Path:
    return get_runtime_home(home) / "data" / "user" / "update" / "source-update"


def backup_root(home: str | Path | None = None) -> Path:
    return get_runtime_home(home) / "data" / "user" / "update-backups"


def launcher_pid() -> int | None:
    raw = os.getenv(LAUNCHER_PID_ENV, "").strip()
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if is_process_alive(pid) else None


@dataclass
class SourceUpdateJob:
    id: str
    status: SourceUpdateJobStatus
    step: str = ""
    error: str = ""
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    backup_dir: str = ""
    pre_sha: str = ""
    post_sha: str = ""
    checkout_root: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "step": self.step,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "backup_dir": self.backup_dir,
            "pre_sha": self.pre_sha,
            "post_sha": self.post_sha,
            "checkout_root": self.checkout_root,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SourceUpdateJob":
        return cls(
            id=str(payload["id"]),
            status=payload["status"],  # type: ignore[arg-type]
            step=str(payload.get("step") or ""),
            error=str(payload.get("error") or ""),
            created_at=str(payload.get("created_at") or ""),
            started_at=str(payload.get("started_at") or ""),
            finished_at=str(payload.get("finished_at") or ""),
            backup_dir=str(payload.get("backup_dir") or ""),
            pre_sha=str(payload.get("pre_sha") or ""),
            post_sha=str(payload.get("post_sha") or ""),
            checkout_root=str(payload.get("checkout_root") or ""),
        )


ACTIVE_STATUSES: frozenset[SourceUpdateJobStatus] = frozenset(
    {"backup", "fetching", "merging", "installing", "restarting"}
)


class SourceUpdateJobStore:
    """Persist one source-update job across the launcher restart boundary."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.state_path = self.root / "state.json"
        self.active_path = self.root / "active"
        self.log_path = self.root / "worker.log"

    def create(self, *, checkout_root: Path) -> SourceUpdateJob:
        self.root.mkdir(parents=True, exist_ok=True)
        job = SourceUpdateJob(
            id=uuid.uuid4().hex,
            status="backup",
            step="Preparing",
            created_at=_now(),
            checkout_root=str(checkout_root.resolve()),
        )
        try:
            descriptor = os.open(self.active_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise SourceUpdateInProgressError("Another update is already in progress") from exc
        try:
            os.write(descriptor, job.id.encode("ascii"))
        finally:
            os.close(descriptor)
        try:
            self._write(job)
        except Exception:
            self.release(job.id)
            raise
        return job

    def load(self) -> SourceUpdateJob:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Invalid source update job")
        return SourceUpdateJob.from_dict(payload)

    def load_or_none(self) -> SourceUpdateJob | None:
        try:
            return self.load()
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def update(self, job_id: str, **fields: Any) -> SourceUpdateJob:
        current = self.load()
        if current.id != job_id:
            raise RuntimeError("Update job changed while it was running")
        updates = dict(fields)
        if "status" in updates:
            timestamp = _now()
            if not current.started_at:
                updates["started_at"] = timestamp
            if updates["status"] in {"succeeded", "failed"}:
                updates["finished_at"] = timestamp
        updated = replace(current, **updates)
        self._write(updated)
        if updated.status in {"succeeded", "failed"}:
            self.release(job_id)
        return updated

    def release(self, job_id: str) -> None:
        try:
            owner = self.active_path.read_text(encoding="ascii")
        except OSError:
            return
        if owner == job_id:
            self.active_path.unlink(missing_ok=True)

    def _write(self, job: SourceUpdateJob) -> None:
        atomic_write_json(self.state_path, job.to_dict())


def launch_source_update_worker(store_root: Path, *, checkout_root: Path) -> None:
    """Launch the trusted worker outside the process tree being replaced."""
    command = [
        sys.executable,
        "-m",
        "deeptutor.runtime.source_update_worker",
        "--store-root",
        str(store_root.resolve()),
        "--checkout-root",
        str(checkout_root.resolve()),
        "--backup-root",
        str(backup_root().resolve()),
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        )
    else:
        kwargs["start_new_session"] = True
    with store_root.joinpath("worker.log").open("a", encoding="utf-8") as log:
        subprocess.Popen(command, stdout=log, **kwargs)  # nosec B603


__all__ = [
    "SourceUpdateInProgressError",
    "SourceUpdateJob",
    "SourceUpdateJobStore",
    "backup_root",
    "checkout_root",
    "launch_source_update_worker",
    "launcher_available",
    "launcher_pid",
    "source_update_store_root",
]
