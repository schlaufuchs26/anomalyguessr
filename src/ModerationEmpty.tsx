/**
 * Empty state of the dev moderation queue (tickets #1202, #1210, #1287).
 *
 * #1202 made an empty queue a calm message instead of a broken game shell.
 * #1210 adds the two things Evan needs there: how deep the pipeline buffer
 * is (unshown accepted scenes the next daily can draw from) and a button to
 * top the queue up on demand, with the run's progress while it works. A
 * finished run reloads the manifest so the new unmoderated scenes appear.
 *
 * That reload is triggered by the running -> done transition observed while
 * polling, not by "the status file says done" (#1287): the file keeps the
 * last run's result forever, so a done run whose scenes never reached this
 * queue (the daily cron ships them, or moderation rejected them) would
 * reload the manifest on every mount, forever.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { type GenerateStatus, loadGenerateStatus, startGenerate } from "./api";

/** Poll interval while a generation run is in flight. */
const POLL_MS = 2000;

/**
 * Human-readable phase of the generator status (ticket #1381). The pipeline
 * writes `phase` per step, so the button area shows what the run is doing
 * instead of a frozen counter.
 */
function phaseLabel(status: GenerateStatus): string {
  const phase = status.phase;
  if (!phase) return "";
  const candidate = status.candidate ?? 0;
  const candidates = status.candidates ?? 0;
  const slot =
    candidate > 0 && candidates > 0
      ? ` candidate ${candidate}/${candidates}`
      : "";
  switch (phase) {
    case "starting":
      return "starting up";
    case "proposing":
      return "proposing an anomaly";
    case "editing":
      return `drawing${slot}`;
    case "checking":
      return `checking${slot}`;
    case "correcting":
      return "applying one correction";
    case "locating":
      return "placing the click target";
    case "scene-done":
      return "scene done";
    case "budget":
      return "generation budget reached";
    default:
      return phase;
  }
}

/** The extra run detail under the counter: phase and running cost (#1381). */
function generationDetail(status: GenerateStatus): string {
  const bits = [phaseLabel(status)];
  if (status.cost && status.cost > 0) bits.push(`$${status.cost.toFixed(2)}`);
  return bits.filter(Boolean).join(" · ");
}

export function ModerationEmpty({ onReload }: { onReload: () => void }) {
  const [status, setStatus] = useState<GenerateStatus | null>(null);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);
  const reload = useRef(onReload);
  reload.current = onReload;
  // True once a read saw a run in flight. Only a run we watched running may
  // trigger the reload when it finishes (#1287); a done result already in
  // the status file at mount is the previous run's, and acting on it would
  // reload the manifest in an endless loop whenever the queue stays empty.
  const sawRunning = useRef(false);

  const refresh = useCallback(async (): Promise<GenerateStatus | null> => {
    try {
      const next = await loadGenerateStatus();
      setStatus(next);
      if (next.state === "error" && next.error) setError(next.error);
      const finished =
        sawRunning.current && !next.running && next.state === "done";
      sawRunning.current = next.running;
      if (finished && next.added > 0) reload.current();
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
      const next = await startGenerate();
      setStatus(next);
      // A run that finishes before the first poll still counts as observed
      // running, so its scenes trigger the reload too.
      sawRunning.current = next.running;
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
  const detail = status ? generationDetail(status) : "";
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
          {detail ? ` · ${detail}` : ""}
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
