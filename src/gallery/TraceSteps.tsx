import {
  fmtDateTime,
  fmtDuration,
  fmtUsage,
  type SceneTrace,
  type TraceStep,
} from "./api";

// Presentational pieces of a generation trace (ticket #1373), extracted from
// TracePanel in ticket #1446 so the live moderation view renders the same
// steps as the gallery's trace panel. Nothing here fetches: the caller owns
// the data (the panel loads the whole sidecar, the live view the poll's
// metadata plus the text of an opened step).

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
export function stageLabel(step: TraceStep): string {
  const stage = step.stage;
  if (STAGE_LABELS[stage]) return STAGE_LABELS[stage];
  const round = /^(edit|fix-edit|check) r(\d)$/.exec(stage);
  const roundLabel = round?.[1] ? ROUND_STAGES[round[1]] : undefined;
  if (roundLabel && round?.[2]) return `${roundLabel} (round ${round[2]})`;
  const click = /^click-target (\d)$/.exec(stage);
  if (click) return `Click-target check ${click[1]}`;
  return stage;
}

/** The model/attempt/tokens line of one step. */
export function TraceStepMeta({ step }: { step: TraceStep }) {
  const bits = [
    step.model,
    step.attempt ? `attempt ${step.attempt}` : "",
    step.draw ? `draw ${step.draw}` : "",
    step.after_fix ? "after correction" : "",
    fmtDuration(step.duration_s),
    fmtUsage(step.usage),
    fmtDateTime(step.at ?? ""),
  ].filter(Boolean);
  return <p className="td-trace-meta">{bits.join(" · ")}</p>;
}

/** What a check or click-target step concluded (#1446): the checker score
 *  with the failed requirement numbers and its one-line reason, or the
 *  click-target verdict. Null for steps that carry no verdict. */
export function TraceStepVerdict({ step }: { step: TraceStep }) {
  const bits: string[] = [];
  if (typeof step.score === "number") {
    bits.push(`checker ${step.score}/8`);
    if (step.score === 8) bits.push("all requirements met");
  }
  if (step.failed && step.failed.length > 0) {
    bits.push(`failed ${step.failed.join(", ")}`);
  }
  if (step.reason) bits.push(step.reason);
  if (step.rejected) bits.push(`gate: ${step.rejected}`);
  if (step.covers === true) bits.push("circle covers the box");
  if (step.covers === false) bits.push("box outside the drawn circle");
  if (step.corrected) bits.push("answer grown to cover it");
  if (step.verdict_reason && !step.reason) bits.push(step.verdict_reason);
  if (bits.length === 0) return null;
  return <p className="td-trace-verdict">{bits.join(" · ")}</p>;
}

/** The step's image name, error and (when the text is loaded) prompt/answer/
 *  reasoning. Empty for a live step whose text has not been fetched yet. */
export function TraceStepBody({
  sceneId,
  step,
  index,
}: {
  sceneId: string;
  step: TraceStep;
  index: number;
}) {
  return (
    <>
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
    </>
  );
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

/** One step of a trace: heading, meta, verdict and body. */
export function TraceStepItem({
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
      <TraceStepMeta step={step} />
      <TraceStepVerdict step={step} />
      <TraceStepBody sceneId={sceneId} step={step} index={index} />
    </li>
  );
}

/** The failure notes of a whole trace: last error, gate rejections, failed
 *  calls. Null when the trace has none. */
export function TraceNotes({ trace }: { trace: SceneTrace }) {
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
