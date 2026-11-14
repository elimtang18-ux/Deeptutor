"""
Detached worker for source-checkout updates.

Started by :func:`deeptutor.services.source_update.launch_source_update_worker`
in its own session so it survives the launcher restart it triggers. Runs the
whole git/deps/restart sequence and records progress in the job store.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from deeptutor.runtime.process import is_process_alive
from deeptutor.services.source_update import (
    SourceUpdateJobStore,
    launcher_pid,
)

MERGE_FAILED_HINT = (
    "本地提交与上游分叉，无法快进合并。请手动执行 git pull --rebase "
    "或 git merge origin/main 处理冲突后再更新。本次未丢弃任何改动。"
)

_NO_RESTART_HINT = "已拉取最新代码并重装依赖，但无法确定 launcher 进程，请手动重启服务。"


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _tail(text: str, limit: int = 1500) -> str:
    return text if len(text) <= limit else f"...\n{text[-limit:]}"


def _resolve_npm() -> str | None:
    found = shutil.which("npm")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/npm", "/usr/local/bin/npm"):
        if os.path.isfile(candidate):
            return candidate
    return None


def _npm_env() -> dict[str, str]:
    env = dict(os.environ)
    path = env.get("PATH", "")
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        if prefix not in path:
            path = f"{prefix}:{path}"
    env["PATH"] = path
    return env


def backup_uncommitted(checkout: Path, backup_dir: Path, job_id: str, store: SourceUpdateJobStore) -> None:
    """Snapshot uncommitted work before anything touches the tree."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    diff = _run(["git", "diff", "HEAD", "--binary"], cwd=checkout)
    patch_path = backup_dir / "local.patch"
    if diff.stdout:
        patch_path.write_text(diff.stdout, encoding="utf-8")
    else:
        patch_path.write_text("# no tracked changes\n", encoding="utf-8")
    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=checkout,
    )
    names = [name for name in untracked.stdout.split("\0") if name]
    skipped: list[str] = []
    for name in names:
        source = checkout / name
        target = backup_dir / "untracked" / name
        try:
            size = source.stat().st_size
        except OSError:
            continue
        if size > 100 * 1024 * 1024:
            skipped.append(f"{name} ({size // (1024 * 1024)} MB)")
            continue
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    lines = [f"backup of uncommitted work, job {job_id}"]
    if names:
        lines.append(f"untracked files: {len(names)}")
    if skipped:
        lines.append("skipped (larger than 100 MB): " + ", ".join(skipped))
    (backup_dir / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fail(store: SourceUpdateJobStore, job_id: str, error: str) -> int:
    try:
        store.update(job_id, status="failed", error=_tail(error))
    except Exception:  # keep the worker honest even if the store is broken
        pass
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepTutor source-checkout update worker")
    parser.add_argument("--store-root", required=True)
    parser.add_argument("--checkout-root", required=True)
    parser.add_argument("--backup-root", required=True)
    args = parser.parse_args()

    checkout = Path(args.checkout_root)
    store = SourceUpdateJobStore(Path(args.store_root))
    job = store.load()
    job_id = job.id
    stash_made = False

    def restore_stash() -> None:
        nonlocal stash_made
        if stash_made:
            _run(["git", "stash", "pop"], cwd=checkout)
            stash_made = False

    try:
        # 1. Back up whatever is uncommitted right now.
        backup_dir = Path(args.backup_root) / time.strftime("%Y%m%d-%H%M%S")
        backup_uncommitted(checkout, backup_dir, job_id, store)
        store.update(job_id, backup_dir=str(backup_dir.resolve()))

        # 2. Stash uncommitted changes so the pull starts from a clean tree.
        if _run(["git", "status", "--porcelain"], cwd=checkout).stdout.strip():
            stashed = _run(
                ["git", "stash", "push", "--include-untracked", "-m", f"deeptutor-update-{job_id[:8]}"],
                cwd=checkout,
            )
            if stashed.returncode != 0:
                return _fail(store, job_id, "git stash 失败：\n" + _tail(stashed.stderr))
            stash_made = True

        # 3. Fetch the latest upstream state.
        store.update(job_id, status="fetching", step="拉取最新代码")
        fetched = _run(["git", "fetch", "origin"], cwd=checkout)
        if fetched.returncode != 0:
            restore_stash()
            return _fail(store, job_id, "git fetch 失败（网络或远端不可达）：\n" + _tail(fetched.stderr))

        upstream = _run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=checkout,
        )
        if upstream.returncode != 0:
            restore_stash()
            return _fail(store, job_id, "当前分支没有配置上游分支，无法自动更新。")

        pre_sha = _run(["git", "rev-parse", "HEAD"], cwd=checkout).stdout.strip()

        # 4. Fast-forward only: never discards local commits.
        store.update(job_id, status="merging", step="合并最新代码")
        merged = _run(["git", "merge", "--ff-only", upstream.stdout.strip()], cwd=checkout)
        if merged.returncode != 0:
            restore_stash()
            return _fail(store, job_id, MERGE_FAILED_HINT + "\n" + _tail(merged.stderr))

        post_sha = _run(["git", "rev-parse", "HEAD"], cwd=checkout).stdout.strip()
        store.update(job_id, pre_sha=pre_sha, post_sha=post_sha)

        if stash_made:
            popped = _run(["git", "stash", "pop"], cwd=checkout)
            if popped.returncode != 0:
                store.update(
                    job_id,
                    step="本地未提交改动未能自动恢复，仍保留在 git stash 中，请手动 git stash pop",
                )
            else:
                stash_made = False

        # 5. Already at the newest commit: nothing to install or restart.
        if pre_sha == post_sha:
            store.update(job_id, status="succeeded", step="已是最新代码，无需更新")
            return 0

        # 6. Reinstall dependencies when their manifests changed.
        py_changed = bool(
            _run(["git", "diff", pre_sha, post_sha, "--", "pyproject.toml", "requirements"], cwd=checkout).stdout.strip()
        )
        web_changed = bool(
            _run(
                ["git", "diff", pre_sha, post_sha, "--", "web/package.json", "web/package-lock.json"],
                cwd=checkout,
            ).stdout.strip()
        )
        if py_changed or web_changed:
            store.update(job_id, status="installing", step="安装依赖")
        if py_changed:
            installed = _run([sys.executable, "-m", "pip", "install", "-e", "."], cwd=checkout)
            if installed.returncode != 0:
                return _fail(store, job_id, "pip install 失败：\n" + _tail(installed.stderr))
        if web_changed:
            npm = _resolve_npm()
            if npm is None:
                return _fail(store, job_id, "未找到 npm，请手动在 web/ 目录执行 npm install。")
            web_root = checkout / "web"
            installed = _run(["npm", "install"], cwd=web_root, env=_npm_env())
            if installed.returncode != 0:
                return _fail(store, job_id, "npm install 失败：\n" + _tail(installed.stderr))

        # 7. Restart the launcher; launchd brings the service back up.
        pid = launcher_pid()
        if pid is None:
            return _fail(store, job_id, _NO_RESTART_HINT)
        store.update(job_id, status="restarting", step="重启服务")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            return _fail(store, job_id, f"向 launcher 发送 SIGTERM 失败：{exc}")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and is_process_alive(pid):
            time.sleep(0.5)
        if is_process_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        store.update(job_id, status="succeeded", step="更新完成，服务已重启")
        return 0
    except Exception as exc:  # noqa: BLE001 - the store must end in a terminal state
        restore_stash()
        return _fail(store, job_id, f"更新过程异常：{exc}")


if __name__ == "__main__":
    raise SystemExit(main())
