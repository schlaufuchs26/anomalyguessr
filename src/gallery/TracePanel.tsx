import { useQuery } from "@tanstack/react-query";
import React from "react";
import { fmtDateTime, getSceneTrace } from "./api";
import { SceneHandle } from "./SceneHandle";
import { TraceNotes, TraceStepItem } from "./TraceSteps";

// Collapsible "Generation trace" panel for the scene lightbox (ticket
// #1373). Collapsed by default and fetched only when opened, so the scene
// list payload stays small and a scene generated before the trace existed
// degrades to "no trace recorded". The trace is publishable text (the
// pipeline strips keys and host paths), so it is shown verbatim.
//
// The step rendering lives in TraceSteps.tsx since ticket #1446: the live
// moderation view shows the same steps for the run in flight.

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
                <TraceStepItem
                  // biome-ignore lint/suspicious/noArrayIndexKey: steps have no stable id; the trace is an ordered log
                  key={`${step.stage}-${i}`}
                  sceneId={sceneId}
                  step={step}
                  index={i}
                  guard={data.score_guard}
                />
              ))}
            </ol>
          </>
        ) : null
      ) : null}
    </section>
  );
}
