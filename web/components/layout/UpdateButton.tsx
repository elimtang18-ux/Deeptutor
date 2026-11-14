"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { CircleCheck, Download, Loader2, TriangleAlert } from "lucide-react";
import { useAuthStatus } from "@/hooks/useAuthStatus";
import {
  fetchAppUpdateStatus,
  fetchSourceUpdateJob,
  requestSourceUpdate,
  sourceUpdateJobIsActive,
  type SourceUpdateJob,
  type SourceUpdateJobStatus,
} from "@/lib/app-update";

type Phase = "confirm" | "running" | "failed" | "done";

export default function UpdateButton() {
  const { t } = useTranslation();
  const { isAdmin } = useAuthStatus();
  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<Phase>("confirm");
  const [job, setJob] = useState<SourceUpdateJob | null>(null);
  const [error, setError] = useState("");
  const [version, setVersion] = useState("");
  const [isSource, setIsSource] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  // Only reload automatically when the success was reached by this panel's
  // own run — not when reopening onto an old finished job.
  const fromRunningRef = useRef(false);

  // Close on outside click or Escape.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  // When opened, load the install info and resume any unfinished job.
  useEffect(() => {
    if (!open || !isAdmin) return;
    let cancelled = false;
    (async () => {
      try {
        const status = await fetchAppUpdateStatus();
        if (cancelled) return;
        setVersion(status.current_version);
        setIsSource(status.installation.mode === "source");
        const existing = await fetchSourceUpdateJob();
        if (cancelled || !existing) return;
        if (sourceUpdateJobIsActive(existing.status)) {
          setJob(existing);
          setPhase("running");
        } else if (existing.status === "failed") {
          setJob(existing);
          setError(existing.error);
          setPhase("failed");
        } else if (existing.status === "succeeded") {
          setJob(existing);
          fromRunningRef.current = false;
          setPhase("done");
        }
      } catch {
        // The panel still works; the status surface just stays empty.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, isAdmin]);

  // Poll the job while it runs; network errors mean the backend is
  // restarting, which is expected — keep polling.
  useEffect(() => {
    if (phase !== "running") return;
    let stopped = false;
    const timer = setInterval(async () => {
      try {
        const latest = await fetchSourceUpdateJob();
        if (stopped || !latest) return;
        setJob(latest);
        if (latest.status === "failed") {
          setError(latest.error);
          setPhase("failed");
        } else if (latest.status === "succeeded") {
          fromRunningRef.current = true;
          setPhase("done");
        }
      } catch {
        // Backend down while restarting; retry on the next tick.
      }
    }, 1000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [phase]);

  // Reload shortly after a successful update so the new build shows up.
  useEffect(() => {
    if (phase !== "done" || !fromRunningRef.current) return;
    const timer = setTimeout(() => window.location.reload(), 1200);
    return () => clearTimeout(timer);
  }, [phase]);

  if (!isAdmin) return null;

  const start = async () => {
    setError("");
    try {
      const created = await requestSourceUpdate();
      setJob(created);
      setPhase("running");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      setPhase("failed");
    }
  };

  const statusLabel = (status: SourceUpdateJobStatus | undefined): string =>
    status ? t(`update.steps.${status}`) : "";

  return (
    <div ref={rootRef} className="dt-update-button relative">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(v => !v)}
        title={t("update.button")}
      >
        <Download aria-hidden />
        <span>{t("update.button")}</span>
      </button>

      {open && (
        <div className="absolute right-0 top-[calc(100%+8px)] z-50 w-[320px] rounded-[10px] border border-[var(--border)] bg-[var(--popover)] p-4 text-[var(--popover-foreground)] shadow-md">
          {phase === "confirm" && (
            <>
              <div className="mb-1 flex items-center gap-2 text-[13px] font-semibold">
                <Download className="h-4 w-4 opacity-70" aria-hidden />
                {t("update.title")}
              </div>
              <p className="mb-3 text-[12.5px] leading-relaxed text-[var(--muted-foreground)]">
                {isSource
                  ? t("update.descriptionSource")
                  : t("update.description")}
              </p>
              {version && (
                <p className="mb-3 text-[12px] text-[var(--muted-foreground)]">
                  {t("update.currentVersion")}: {version}
                </p>
              )}
              {!isSource && (
                <p className="mb-3 text-[12px] text-amber-600 dark:text-amber-400">
                  {t("update.notSource")}
                </p>
              )}
              <button
                type="button"
                onClick={start}
                disabled={!isSource}
                className="inline-flex h-8 items-center gap-2 rounded-md bg-[var(--primary)] px-3 text-[12.5px] font-medium text-[var(--primary-foreground)] transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {t("update.confirm")}
              </button>
            </>
          )}

          {phase === "running" && (
            <div className="flex items-start gap-2.5">
              <Loader2 className="mt-0.5 h-4 w-4 animate-spin opacity-70" aria-hidden />
              <div className="min-w-0">
                <p className="text-[13px] font-medium">
                  {statusLabel(job?.status) || t("update.running")}
                </p>
                {job?.step && (
                  <p className="mt-1 break-words text-[12px] leading-relaxed text-[var(--muted-foreground)]">
                    {job.step}
                  </p>
                )}
              </div>
            </div>
          )}

          {phase === "failed" && (
            <div className="flex items-start gap-2.5">
              <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-red-500" aria-hidden />
              <div className="min-w-0 flex-1">
                <p className="text-[13px] font-medium">{t("update.failed")}</p>
                <p className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words text-[12px] leading-relaxed text-[var(--muted-foreground)]">
                  {error}
                </p>
                {job?.backup_dir && (
                  <p className="mt-1 break-all text-[12px] text-[var(--muted-foreground)]">
                    {t("update.backup")} {job.backup_dir}
                  </p>
                )}
                <button
                  type="button"
                  onClick={start}
                  className="mt-2 inline-flex h-8 items-center gap-2 rounded-md border border-[var(--border)] px-3 text-[12.5px] font-medium hover:bg-[var(--accent)]"
                >
                  {t("update.retry")}
                </button>
              </div>
            </div>
          )}

          {phase === "done" && (
            <div className="flex items-center justify-between gap-2.5">
              <div className="flex min-w-0 items-center gap-2.5">
                <CircleCheck className="h-4 w-4 shrink-0 text-green-600" aria-hidden />
                <p className="truncate text-[13px]">
                  {t("update.done")} {job?.step || ""}
                </p>
              </div>
              <button
                type="button"
                onClick={() => window.location.reload()}
                className="inline-flex h-8 shrink-0 items-center gap-2 rounded-md border border-[var(--border)] px-3 text-[12.5px] font-medium hover:bg-[var(--accent)]"
              >
                {t("update.refresh")}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
