import { useQuery } from "@tanstack/react-query";
import React from "react";
import {
  fmtDateTime,
  fmtDuration,
  fmtUsage,
  getSceneTrace,
  type SceneTrace,
  type TraceStep,
} from "./api";
import { SceneHandle } from "./SceneHandle";

// Collapsible "Generation trace" panel for the scene lightbox (ticket
// #1373). Collapsed by default and fetched only when opened, so the scene
// list payload stays small and a scene generated before the trace existed
// degrades to "no trace recorded". The trace is publishable text (the
// pipeline strips keys and host paths), so it is shown verbatim.

/** Human labels for the pipeline's step names (ticket #1372's flow). */
const STAGE_LABELS: Record<string, string> = {
  proposal: "Anomaly proposal",
  // Legacy pre-#1436 stage names still readable on old traces.
  edit: "Image edit",
  coordinates: "Locate click target",
  check: "Quality check",
  "fix-edit": "Correction edit",
  recheck: "Re-check after correction",
};

const ROUND_STAGES: Record<string, string> = {
  edit: "Image edit",
  "fix-edit": "Correction edit",
  check: "Quality check",
};

/** Stage label incl. the round (#1436: "edit r0", "check r1", ...). */
function stageLabel(step: TraceStep): string {
  const stage = step.stage;
  if (STAGE_LABELS[stage]) return STAGE_LABELS[stage];
  const round = /^(edit|fix-edit|check) r(\d)$/.exec(stage);
  const roundLabel = round?.[1] ? ROUND_STAGES[round[1]] : undefined;
  if (roundLabel && round?.[2]) return `${roundLabel} (round ${round[2]})`;
  const click = /^click-target (\d)$/.exec(stage);
  if (click) return `Click-target check ${click[1]}`;
  return stage;
}

function StepMeta({ step }: { step: TraceStep }) {
  const bits = [
    step.model,
    step.attempt ? `attempt ${step.attempt}` : "",
    step.after_fix ? "after correction" : "",
    fmtDuration(step.duration_s),
    fmtUsage(step.usage),
    fmtDateTime(step.at ?? ""),
  ].filter(Boolean);
  return <p className="td-trace-meta">{bits.join(" · ")}</p>;
}

function StepText({ label, text }: { label: string; text: string }) {
  if (!text) return null;
  return (
    <>
      <p className="td-trace-label">{label}</p>
      <pre className="td-trace-text">{text}</pre>
    </>
  );
}

function Step({
  sceneId,
  step,
  index,
}: {
  sceneId: string;
  step: TraceStep;
  index: number;
}) {
  return (
    <li
      className="td-trace-step"
      data-testid={`td-trace-step-${sceneId}-${index}`}
    >
      <h4 className="td-trace-stage">
        <span className="td-trace-index">{index + 1}</span>
        {stageLabel(step)}
      </h4>
      <StepMeta step={step} />
      {step.image ? (
        <p className="td-trace-image">image: {step.image}</p>
      ) : null}
      {step.error ? (
        <p
          className="td-trace-error"
          data-testid={`td-trace-error-${sceneId}-${index}`}
        >
          {step.error}
        </p>
      ) : null}
      <StepText label="Prompt" text={step.prompt ?? ""} />
      <StepText label="Answer" text={step.answer ?? ""} />
      <StepText label="Reasoning" text={step.reasoning ?? ""} />
    </li>
  );
}

function TraceNotes({ trace }: { trace: SceneTrace }) {
  const notes: string[] = [];
  if (trace.error) notes.push(`last error: ${trace.error}`);
  for (const g of trace.gate_failures ?? [])
    notes.push(`gate rejected attempt ${g.attempt ?? "?"}: ${g.reason ?? ""}`);
  for (const c of trace.call_errors ?? [])
    notes.push(
      `${c.stage ?? "call"}${c.after_fix ? " (after fix)" : ""} failed: ${c.error ?? ""}`,
    );
  if (notes.length === 0) return null;
  return (
    <ul className="td-trace-notes" data-testid="td-trace-notes">
      {notes.map((n) => (
        <li key={n}>{n}</li>
      ))}
    </ul>
  );
}

/**
 * The scene's generation trace. `hasTrace` gates the request: a scene
 * without a sidecar shows the empty note instead of a 404 fetch.
 */
export function TracePanel({
  sceneId,
  shortId,
  hasTrace,
}: {
  sceneId: string;
  /** Short handle shown next to the toggle (ticket #1413). */
  shortId?: string | undefined;
  hasTrace: boolean;
}) {
  const [open, setOpen] = React.useState(false);
  const { data, isLoading, error } = useQuery({
    queryKey: ["td-trace", sceneId],
    queryFn: () => getSceneTrace(sceneId),
    enabled: open && hasTrace,
    retry: false,
  });

  const toggle = () => setOpen((o) => !o);

  return (
    <section className="td-trace" data-testid={`td-trace-${sceneId}`}>
      <button
        type="button"
        className="td-trace-toggle"
        onClick={toggle}
        aria-expanded={open}
        disabled={!hasTrace}
        data-testid={`td-trace-toggle-${sceneId}`}
      >
        {open ? "▾" : "▸"} Generation trace
      </button>
      <SceneHandle shortId={shortId} id={sceneId} placement="trace" />
      {hasTrace ? null : (
        <p className="td-trace-empty" data-testid={`td-trace-empty-${sceneId}`}>
          no trace recorded
        </p>
      )}
      {open && hasTrace ? (
        isLoading ? (
          <p className="loading" data-testid={`td-trace-loading-${sceneId}`}>
            Loading trace...
          </p>
        ) : error ? (
          <p
            className="td-trace-error"
            data-testid={`td-trace-loaderror-${sceneId}`}
          >
            Failed to load trace: {String(error)}
          </p>
        ) : data ? (
          <>
            <p className="td-trace-head">
              {data.calls.length} step{data.calls.length === 1 ? "" : "s"}
              {data.model ? ` · ${data.model}` : ""}
              {data.image_model ? ` + ${data.image_model}` : ""}
              {data.recordedAt
                ? ` · recorded ${fmtDateTime(data.recordedAt)}`
                : ""}
            </p>
            <TraceNotes trace={data} />
            <ol className="td-trace-steps">
              {data.calls.map((step, i) => (
                <Step
                  // biome-ignore lint/suspicious/noArrayIndexKey: steps have no stable id; the trace is an ordered log
                  key={`${step.stage}-${i}`}
                  sceneId={sceneId}
                  step={step}
                  index={i}
                />
              ))}
            </ol>
          </>
        ) : null
      ) : null}
    </section>
  );
}
