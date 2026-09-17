/**
 * The "Generate more" control of the moderation queue (tickets #1210, #1446,
 * #1584).
 *
 * It polls the generator status, shows how deep the pipeline buffer is (the
 * unshown accepted scenes the next daily can draw from), offers a button to
 * top the queue up on demand, and renders the run's progress and its pipeline
 * steps while it works (#1446). A finished run the control watched running
 * hands back to the caller via `onFinished`, so the caller reloads or merges
 * the new scenes.
 *
 * #1210 put this inside the empty-queue state, which made the button vanish
 * as soon as scenes were queued. #1584 moves the control into its own
 * component so the populated moderation view can show it too: the button and
 * the buffer count stay visible regardless of queue depth, while only the
 * empty state carries the calm "queue is empty" copy.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { type GenerateStatus, loadGenerateStatus, startGenerate } from "./api";
import { LiveTraceView } from "./LiveTrace";

/** Poll interval while a generation run is in flight. */
export const DEFAULT_GENERATE_POLL_MS = 2000;

/**
 * Human-readable phase of the generator status (ticket #1381). The pipeline
 * writes `phase` per step, so the button area shows what the run is doing
 * instead of a frozen counter.
 */
function phaseLabel(status: GenerateStatus): string {
  const phase = status.phase;
  if (!phase) return "";
  const round = status.round ?? 0;
  const roundLabel = round > 0 ? ` (round ${round} of 2)` : "";
  const passLabel =
    (status.clickPass ?? 0) > 0 ? ` (pass ${status.clickPass})` : "";
  switch (phase) {
    case "starting":
      return "starting up";
    case "proposing":
      return "proposing an anomaly";
    case "editing":
      return "drawing the scene image";
    case "checking":
      return `checking the result${roundLabel}`;
    case "correcting":
      return `applying a correction${roundLabel}`;
    case "locating":
      return "placing the click target";
    case "click-target":
      return `checking the click target${passLabel}`;
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

/** The buffer line; the count Evan wants next to the button. */
function bufferLabel(buffer: number): string {
  return `${buffer} ${buffer === 1 ? "scene" : "scenes"} ready for the daily`;
}

export function GenerateControl({
  onFinished,
  onReload,
  pollMs = DEFAULT_GENERATE_POLL_MS,
}: {
  /**
   * Called when a run this control watched running finishes with new scenes
   * (`added > 0`). The empty state reloads the manifest there; the populated
   * view merges the new scenes without disturbing the queue in progress
   * (#1584).
   */
  onFinished?: () => void;
  /** The Reload button's action; omit it to render the button without one. */
  onReload?: () => void;
  /** Poll cadence while a run is in flight; tests drive a shorter one. */
  pollMs?: number;
}) {
  const [status, setStatus] = useState<GenerateStatus | null>(null);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);
  const finished = useRef(onFinished);
  finished.current = onFinished;
  // True once a read saw a run in flight. Only a run we watched running may
  // trigger the finished callback (#1287): a done result already in the
  // status file at mount is the previous run's, and acting on it would fire
  // the callback in an endless loop whenever the queue stays empty.
  const sawRunning = useRef(false);

  const refresh = useCallback(async (): Promise<GenerateStatus | null> => {
    try {
      const next = await loadGenerateStatus();
      setStatus(next);
      if (next.state === "error" && next.error) setError(next.error);
      const done = sawRunning.current && !next.running && next.state === "done";
      sawRunning.current = next.running;
      if (done && next.added > 0) finished.current?.();
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
    const timer = window.setInterval(() => void refresh(), pollMs);
    return () => window.clearInterval(timer);
  }, [status?.running, refresh, pollMs]);

  const onGenerate = async () => {
    setStarting(true);
    setError("");
    try {
      const next = await startGenerate();
      setStatus(next);
      // A run that finishes before the first poll still counts as observed
      // running, so its scenes trigger the finished callback too.
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
    <div className="gen-control" data-testid="generate-control">
      {running ? (
        <p className="gen-progress" role="status">
          Generating… {processed} / {total}
          {detail ? ` · ${detail}` : ""}
        </p>
      ) : null}
      <LiveTraceView
        trace={status?.liveTrace}
        running={running}
        endState={status?.state ?? "idle"}
        sceneId={status?.sceneId}
      />
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
        <span className="buffer" data-testid="gen-buffer">
          {status ? bufferLabel(buffer) : "Reading the buffer…"}
        </span>
        {onReload ? (
          <button
            id="reload-btn"
            className="btn"
            type="button"
            onClick={() => onReload()}
          >
            Reload
          </button>
        ) : null}
      </div>
    </div>
  );
}
