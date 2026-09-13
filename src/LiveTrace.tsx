import { useCallback, useEffect, useRef, useState } from "react";
import type { LiveTrace } from "./api";
import { getSceneTrace, type SceneTrace, type TraceStep } from "./gallery/api";
import {
  stageLabel,
  TraceStepBody,
  TraceStepMeta,
  TraceStepVerdict,
} from "./gallery/TraceSteps";

/**
 * The steps of the generation run in flight (ticket #1446), shown under the
 * moderation queue's Generate button.
 *
 * The data comes in two layers. The poll of GET /generate carries the step
 * metadata of the scene the pipeline is on (stage, model, duration, tokens,
 * cost, checker score, click-target verdict); its large text fields
 * (prompt/answer/reasoning) stay out of that payload. Opening one step
 * fetches the trace from GET /traces/{ref} and shows that step's text. The
 * rendering is the gallery's trace rendering (TraceSteps.tsx), so the live
 * view and the lightbox's trace panel look the same.
 *
 * Two states the server cannot express are handled here:
 *  - the run goes quiet (the final status write drops the active scene, or
 *    the run dies): the last steps stay visible instead of vanishing, and a
 *    dead run shows the error `endState` the caller passes;
 *  - the scene lands while the run continues: the pending file is renamed to
 *    the scene id, so the text then loads from `sceneId` (the finished
 *    sidecar) instead of the pending ref.
 */
export function LiveTraceView({
  trace,
  running,
  endState,
  sceneId,
}: {
  /** The poll's live trace, undefined while no scene is being worked on. */
  trace: LiveTrace | undefined;
  running: boolean;
  /** The run's terminal state once it is over; "running" while it is live. */
  endState: "running" | "done" | "error" | "idle";
  /** Long id of the last scene that landed: its finished trace sidecar. */
  sceneId?: string | undefined;
}) {
  // The last steps we saw. The server stops sending them when the run ends
  // (or dies), and the view must not go blank then.
  const [shown, setShown] = useState<LiveTrace | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [full, setFull] = useState<SceneTrace | null>(null);
  const [textError, setTextError] = useState("");
  const listRef = useRef<HTMLOListElement>(null);

  useEffect(() => {
    if (!trace) return;
    setShown((current) => {
      // A new ref is a new scene (or a new run): start over with its steps.
      if (current && current.ref !== trace.ref) return trace;
      // Same scene: keep the longer list, so a half-written read cannot
      // shrink the view.
      if (current && current.steps.length > trace.steps.length) return current;
      return trace;
    });
  }, [trace]);

  // Keep the newest step in view. The count is the trigger: the list is
  // appended to in place, so the scroll has to follow each new step.
  const stepCount = shown?.steps.length ?? 0;
  useEffect(() => {
    const list = listRef.current;
    if (list && stepCount > 0) list.scrollTop = list.scrollHeight;
  }, [stepCount]);

  const textRef = running || !sceneId ? (shown?.ref ?? null) : sceneId;

  /** Toggle one step; load the trace's text the first time a step opens. */
  const toggleStep = useCallback(
    (index: number) => {
      setOpen((current) => (current === index ? null : index));
      if (!textRef || (full && full.calls.length > index)) return;
      void getSceneTrace(textRef)
        .then((loaded) => {
          setFull(loaded);
          setTextError("");
        })
        .catch((err) => setTextError(String(err)));
    },
    [textRef, full],
  );

  if (!shown || shown.steps.length === 0) {
    if (!running) return null;
    return (
      <p className="live-trace-waiting" data-testid="live-trace-waiting">
        Waiting for the first pipeline step…
      </p>
    );
  }

  const badge = running ? "live" : endState === "error" ? "failed" : "finished";
  return (
    <section className="live-trace" data-testid="live-trace">
      <p className="live-trace-head">
        <span
          className={`live-trace-badge live-trace-badge-${badge}`}
          data-testid="live-trace-state"
        >
          {badge}
        </span>
        {shown.steps.length} pipeline step
        {shown.steps.length === 1 ? "" : "s"}
        {running ? "" : " · run ended"}
      </p>
      {shown.error ? <p className="td-trace-error">{shown.error}</p> : null}
      <ol className="td-trace-steps live-trace-steps" ref={listRef}>
        {shown.steps.map((step: TraceStep, index) => (
          <li
            // biome-ignore lint/suspicious/noArrayIndexKey: steps are an ordered log without ids
            key={index}
            className="td-trace-step"
            data-testid={`live-step-${index}`}
          >
            <button
              type="button"
              className="td-trace-toggle"
              onClick={() => toggleStep(index)}
              aria-expanded={open === index}
              data-testid={`live-step-toggle-${index}`}
            >
              {open === index ? "▾" : "▸"} {index + 1}. {stageLabel(step)}
            </button>
            <TraceStepMeta step={step} />
            <TraceStepVerdict step={step} />
            {open === index ? (
              <>
                <TraceStepBody
                  sceneId="live"
                  step={full?.calls[index] ?? step}
                  index={index}
                />
                {textError ? (
                  <p
                    className="td-trace-error"
                    data-testid="live-step-text-error"
                  >
                    Failed to load the step text: {textError}
                  </p>
                ) : null}
              </>
            ) : null}
          </li>
        ))}
      </ol>
    </section>
  );
}
