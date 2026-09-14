// biome-ignore-all lint/a11y/noStaticElementInteractions: the lightbox closes
// on a click on the backdrop (the panel swallows its own clicks). Every
// dismissal is also reachable through the ✕ button, so the overlay click stays
// a mouse convenience on a role="presentation" element.
import React from "react";
import { AnswerOverlay, isAnswerCircle } from "./answer";
import { checkerLabel, fmtDate, fmtDateTime, type TDScene } from "./api";
import { SceneHandle } from "./SceneHandle";
import { TracePanel } from "./TracePanel";

type ImgTab = "edited" | "original" | "audit";

/** Image lightbox for one queue scene: edited / original / audit crop tabs,
 *  the answer click-area overlay (ticket #1209) plus the provenance +
 *  answer details. */
export function SceneModal({
  scene,
  onClose,
}: {
  scene: TDScene;
  onClose: () => void;
}) {
  const [tab, setTab] = React.useState<ImgTab>("edited");
  // The click area is on by default (ticket #1209): checking the answer
  // position is the usual reason to open a scene, and one click hides it
  // again when judging aesthetics.
  const [showAnswer, setShowAnswer] = React.useState(true);

  const hasAnswer = isAnswerCircle(scene.answer);
  // The audit crop is a zoomed detail of the hotspot, so the answer's
  // normalized coordinates do not map onto it.
  const framesAnswer = tab === "edited" || tab === "original";

  // A tuple type keeps the first tab non-optional under
  // noUncheckedIndexedAccess, so the fallback below needs no assertion.
  type ImageTab = { tab: ImgTab; label: string; url: string; note: string };
  const images: [ImageTab, ...ImageTab[]] = [
    {
      tab: "edited",
      label: "Scene",
      url: scene.images.edited,
      note: "Edited scene: the anomaly is planted here.",
    },
    {
      tab: "original",
      label: "Original",
      url: scene.images.original,
      note: "Untouched source photo (pre-1928 / licensed).",
    },
    ...(scene.images.audit
      ? [
          {
            tab: "audit" as const,
            label: "Audit crop",
            url: scene.images.audit,
            note: "The verify pipeline's hotspot crop: evidence for the answer position (no click-area overlay: zoomed detail).",
          },
        ]
      : []),
  ];
  const current = images.find((i) => i.tab === tab) ?? images[0];

  return (
    <div
      className="modal-overlay td-modal-overlay"
      role="presentation"
      onClick={(e) => {
        // Dismiss on the backdrop only; the panel stays a plain div.
        if (e.target === e.currentTarget) onClose();
      }}
      data-testid={`td-modal-overlay-${scene.id}`}
    >
      <div
        className="modal td-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`${scene.title} · ${scene.place}, ${scene.year}`}
      >
        <div className="modal-header">
          <h3 className="modal-title">
            {scene.title} · {scene.place}, {scene.year}
          </h3>
          <SceneHandle
            shortId={scene.shortId}
            id={scene.id}
            placement="modal"
          />
          <button
            type="button"
            className="modal-close"
            onClick={onClose}
            data-testid={`td-modal-close-${scene.id}`}
          >
            ✕
          </button>
        </div>
        <div className="modal-body td-modal-body">
          <div className="td-tabs-row">
            <div className="td-tabs" role="tablist" aria-label="Image views">
              {images.map((i) => (
                <button
                  key={i.tab}
                  type="button"
                  role="tab"
                  aria-selected={tab === i.tab}
                  className={`td-tab${tab === i.tab ? " active" : ""}`}
                  onClick={() => setTab(i.tab)}
                  data-testid={`td-imgtab-${i.tab}-${scene.id}`}
                >
                  {i.label}
                </button>
              ))}
            </div>
            <div className="td-view-tools">
              {hasAnswer ? (
                <label className="td-answer-toggle">
                  <input
                    type="checkbox"
                    checked={showAnswer}
                    disabled={!framesAnswer}
                    onChange={(e) => setShowAnswer(e.target.checked)}
                    data-testid={`td-answer-toggle-${scene.id}`}
                  />
                  <span>Click area</span>
                </label>
              ) : null}
              {/* Handle in the image view itself (ticket #1433): the modal
                  header scrolls out of the body's scroll area, and a corner
                  overlay on the photo would sit on the anomaly being
                  judged. The tab-row slot keeps it on screen while the
                  image is. */}
              <SceneHandle
                shortId={scene.shortId}
                id={scene.id}
                placement="view"
              />
            </div>
          </div>
          <figure className="td-figure">
            <div className="td-img-wrap">
              <img
                src={current.url}
                alt={`${scene.title} (${current.label})`}
                data-testid={`td-img-${scene.id}`}
              />
              <AnswerOverlay
                answer={scene.answer}
                visible={showAnswer && framesAnswer}
              />
            </div>
            <figcaption>{current.note}</figcaption>
          </figure>
          <dl className="td-details">
            <div>
              <dt>Anomaly</dt>
              <dd>{scene.anomaly}</dd>
            </div>
            <div>
              <dt>Title</dt>
              <dd data-testid={`td-modal-title-source-${scene.id}`}>
                {scene.titleSource === "catalog"
                  ? "catalogue name"
                  : scene.titleSource === "fallback"
                    ? "Photograph placeholder"
                    : "not recorded"}
              </dd>
            </div>
            <div>
              <dt>Answer</dt>
              <dd>
                {isAnswerCircle(scene.answer)
                  ? `x=${scene.answer.x.toFixed(2)}, y=${scene.answer.y.toFixed(2)}, r=${scene.answer.r.toFixed(2)}`
                  : "no answer data"}
              </dd>
            </div>
            <div>
              <dt>Checker</dt>
              <dd data-testid={`td-modal-checker-${scene.id}`}>
                {scene.checker
                  ? `${checkerLabel(scene.checker)}${scene.checker.reason ? ` · ${scene.checker.reason}` : ""}`
                  : "no checker verdict recorded"}
                {scene.needsReview ? " · needs review" : ""}
              </dd>
            </div>
            <div>
              <dt>Source</dt>
              <dd>
                <a
                  href={scene.sourceUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {scene.credit || scene.sourceUrl}
                </a>
              </dd>
            </div>
            <div>
              <dt>Dates</dt>
              <dd>
                added {fmtDate(scene.added)}
                {scene.shown ? ` · shown ${fmtDate(scene.shown)}` : ""}
                {scene.rejectedAt
                  ? ` · rejected ${fmtDateTime(scene.rejectedAt)}`
                  : ""}
              </dd>
            </div>
            {scene.description ? (
              <div className="td-detail-wide">
                <dt>Description</dt>
                <dd>{scene.description}</dd>
              </div>
            ) : null}
          </dl>
          <TracePanel
            sceneId={scene.id}
            shortId={scene.shortId}
            hasTrace={scene.hasTrace}
          />
        </div>
      </div>
    </div>
  );
}
