/**
 * Empty state of the dev moderation queue (tickets #1202 + #1210).
 *
 * #1202 made an empty queue a calm message instead of a broken game shell.
 * #1210 adds the two things Evan needs there: how deep the pipeline buffer
 * is (unshown accepted scenes the next daily can draw from) and a button to
 * top the queue up on demand, with the run's progress while it works. A
 * finished run reloads the manifest so the new unmoderated scenes appear.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { type GenerateStatus, loadGenerateStatus, startGenerate } from "./api";

/** Poll interval while a generation run is in flight. */
const POLL_MS = 2000;

export function ModerationEmpty({ onReload }: { onReload: () => void }) {
  const [status, setStatus] = useState<GenerateStatus | null>(null);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);
  const reload = useRef(onReload);
  reload.current = onReload;
  // A run that added scenes reloads the manifest once; after that the app
  // either plays them (this component unmounts) or shows a fresh empty state.
  const reloaded = useRef(false);

  const refresh = useCallback(async (): Promise<GenerateStatus | null> => {
    try {
      const next = await loadGenerateStatus();
      setStatus(next);
      if (next.state === "error" && next.error) setError(next.error);
      if (next.state === "done" && next.added > 0 && !reloaded.current) {
        reloaded.current = true;
        reload.current();
      }
      return next;
    } catch (err) {
      setError(String(err));
      return null;
    }
  }, []);

  // The run may already be in flight (another tab, or the daily cron).
  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Poll while running so the counter keeps moving.
  useEffect(() => {
    if (!status?.running) return;
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [status?.running, refresh]);

  const onGenerate = async () => {
    setStarting(true);
    setError("");
    try {
      setStatus(await startGenerate());
    } catch (err) {
      setError(String(err));
    } finally {
      setStarting(false);
    }
  };

  const running = status?.running ?? false;
  const total = status?.planned || status?.count || 0;
  const processed = (status?.added ?? 0) + (status?.failed ?? 0);
  const buffer = status?.buffer ?? 0;
  return (
    <section id="empty" className="empty-state">
      <h2>Moderation queue is empty</h2>
      <p>
        Nothing to review right now. New scenes appear after the daily
        generation run.
      </p>
      <p className="buffer">
        {status
          ? `${buffer} ${buffer === 1 ? "scene" : "scenes"} ready for the daily`
          : "Reading the buffer…"}
      </p>
      {running ? (
        <p className="gen-progress" role="status">
          Generating… {processed} / {total}
        </p>
      ) : null}
      {error ? (
        <p className="gen-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="empty-actions">
        <button
          id="generate-btn"
          className="btn primary"
          type="button"
          disabled={running || starting}
          onClick={() => void onGenerate()}
        >
          {running ? "Generating…" : "Generate more"}
        </button>
        <button
          id="reload-btn"
          className="btn"
          type="button"
          onClick={() => onReload()}
        >
          Reload
        </button>
      </div>
    </section>
  );
}
