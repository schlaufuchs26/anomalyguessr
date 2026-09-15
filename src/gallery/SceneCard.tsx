import { useMutation, useQueryClient } from "@tanstack/react-query";
import React from "react";
import {
  agUrl,
  checkerLabel,
  defectLabels,
  fmtDate,
  fmtDateTime,
  MOD_LABELS,
  pointsLabel,
  QUERY_KEY,
  showsNeedsReview,
  type TDComment,
  type TDScene,
} from "./api";
import { SceneHandle } from "./SceneHandle";
import { TAGS, TagChip, TagToggles } from "./TagToggles";

/** One queue scene card: edited image (opens the lightbox), provenance meta,
 *  moderation badge, an optional "Daily N" marker (#1208) and the
 *  reject/restore + feedback controls. */
export function SceneCard({
  scene,
  onShow,
  dailyRank,
}: {
  scene: TDScene;
  onShow: (scene: TDScene) => void;
  /** 1-based position in the upcoming daily set (#1208); renders a marker. */
  dailyRank?: number;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = React.useState("");
  const [error, setError] = React.useState("");

  const invalidate = React.useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
  }, [queryClient]);

  const setRejected = useMutation({
    mutationFn: async (rejected: boolean) => {
      const action = rejected ? "reject" : "restore";
      const res = await fetch(agUrl(`scenes/${scene.id}/${action}`), {
        method: "POST",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json() as Promise<{ id: string; rejected: boolean }>;
    },
    onSuccess: invalidate,
    onError: (e: Error) => setError(e.message),
  });

  const addComment = useMutation({
    mutationFn: async (text: string) => {
      const res = await fetch(agUrl(`scenes/${scene.id}/comments`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json() as Promise<{ comment: TDComment }>;
    },
    onSuccess: () => {
      setDraft("");
      setError("");
      invalidate();
    },
    onError: (e: Error) => setError(e.message),
  });

  const submitComment = (e: React.FormEvent) => {
    e.preventDefault();
    const text = draft.trim();
    if (!text || addComment.isPending) return;
    addComment.mutate(text);
  };

  const toggle = () => {
    setError("");
    setRejected.mutate(!scene.rejected);
  };

  const defects = defectLabels(scene.mechanical);
  const points = scene.checker ? pointsLabel(scene.checker) : null;

  return (
    <article
      className={`td-card td-state-${scene.state}`}
      data-testid={`td-card-${scene.id}`}
    >
      <button
        type="button"
        className="td-card-image"
        onClick={() => onShow(scene)}
        title="View scene, original and audit crop"
      >
        <img
          src={scene.images.edited}
          alt={`${scene.title} (edited scene)`}
          loading="lazy"
        />
      </button>
      <div className="td-card-body">
        <div className="td-card-head">
          <h3 className="td-card-title">{scene.title}</h3>
          <div className="td-card-badges">
            <SceneHandle
              shortId={scene.shortId}
              id={scene.id}
              placement="card"
            />
            {dailyRank ? (
              <span
                className="td-daily-rank"
                title="Position in the upcoming daily set"
                data-testid={`td-daily-${scene.id}`}
              >
                Daily {dailyRank}
              </span>
            ) : null}
            <span
              className={`td-badge td-mod-${scene.moderation}`}
              data-testid={`td-moderation-${scene.id}`}
            >
              {MOD_LABELS[scene.moderation]}
            </span>
          </div>
        </div>
        <div className="td-card-meta">
          <span>
            {scene.place}, {scene.year}
          </span>
          <span className="td-card-dates">
            added {fmtDate(scene.added)}
            {scene.shown ? ` · shown ${fmtDate(scene.shown)}` : ""}
            {scene.rejectedAt ? (
              <span
                className="td-card-rejected"
                data-testid={`td-rejected-at-${scene.id}`}
              >
                {` · rejected ${fmtDateTime(scene.rejectedAt)}`}
              </span>
            ) : null}
          </span>
        </div>
        <div className="td-card-chips">
          <span className="td-chip td-chip-anomaly" title="The anomaly">
            {scene.anomaly}
          </span>
          {showsNeedsReview(scene) ? (
            <span
              className="td-chip td-chip-review"
              title="The checker found a problem the pipeline could not repair; review before accepting"
              data-testid={`td-review-${scene.id}`}
            >
              needs review
            </span>
          ) : null}
          {scene.checker ? (
            <span
              className={
                scene.checker.failed.length === 0
                  ? "td-chip td-chip-checker td-checker-clean"
                  : "td-chip td-chip-checker"
              }
              title={
                scene.checker.reason ||
                "How many of the 8 requirements the shipped image met"
              }
              data-testid={`td-checker-${scene.id}`}
            >
              {checkerLabel(scene.checker)}
            </span>
          ) : null}
          {points ? (
            <span
              className="td-chip td-chip-points"
              title="Soft criteria met, one point each (ticket #1502); hard defects are counted separately and never offset these"
              data-testid={`td-points-${scene.id}`}
            >
              {points}
            </span>
          ) : null}
          {TAGS.map((t) => (
            <TagChip
              key={t.tag}
              scene={scene}
              tag={t.tag}
              chip={t.chip}
              title={t.title}
            />
          ))}
        </div>
        {defects.length > 0 ? (
          <p
            className="td-defects"
            title="Hard mechanical defects; points never cancel them"
            data-testid={`td-defects-${scene.id}`}
          >
            ⚠ {defects.join(" · ")}
          </p>
        ) : null}

        <div className="td-card-actions">
          <TagToggles scene={scene} />
          {scene.rejected ? (
            <button
              type="button"
              className="td-restore"
              onClick={toggle}
              disabled={setRejected.isPending}
              data-testid={`td-restore-${scene.id}`}
            >
              {setRejected.isPending ? "Restoring…" : "↩ Restore"}
            </button>
          ) : (
            <button
              type="button"
              className="td-reject"
              onClick={toggle}
              disabled={setRejected.isPending}
              data-testid={`td-reject-${scene.id}`}
              title="Never ship this scene again"
            >
              {setRejected.isPending ? "Rejecting…" : "✕ Reject"}
            </button>
          )}
        </div>

        {scene.comments.length > 0 ? (
          <ul className="td-comments">
            {scene.comments.map((c) => (
              <li key={`${c.createdAt}-${c.text}`} className="td-comment">
                <span className="td-comment-text">{c.text}</span>
                <span className="td-comment-date">
                  {fmtDateTime(c.createdAt)}
                </span>
              </li>
            ))}
          </ul>
        ) : null}

        <form className="td-comment-form" onSubmit={submitComment}>
          <input
            type="text"
            className="td-comment-input"
            placeholder="Feedback to the pipeline…"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            data-testid={`td-comment-input-${scene.id}`}
            maxLength={2000}
          />
          <button
            type="submit"
            className="td-comment-submit"
            disabled={addComment.isPending || draft.trim() === ""}
            data-testid={`td-comment-submit-${scene.id}`}
          >
            Add
          </button>
        </form>
        {error ? <p className="td-error">{error}</p> : null}
      </div>
    </article>
  );
}
