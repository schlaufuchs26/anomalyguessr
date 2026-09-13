import type { AnswerCircle } from "./api";

// Answer click-area overlay for the gallery's image lightbox (ticket #1209).
// The answer of a scene is a circle in normalized image coordinates (x/y
// center, r radius, all 0..1); that is the region the game scores a click
// against (`resolveGuess` -> scoring.ts `clickDistance`/`isHit`). The gallery
// used to show only the raw numbers, so Evan judged placement blind. The
// helpers below keep the drawn region and the hit test in one coordinate
// space; the component is the presentation shell around them.

/** The circle's CSS box as percentages of the rendered image: x/y center and
 *  r radius reproduce exactly the game's geometry. Percent width and height
 *  differ on a non-square image, so in pixel space the overlay is an ellipse;
 *  in normalized image space (where the hit test lives) it is the circle
 *  scoring.ts scores against. Rounding to 0.01 % (sub-pixel on any realistic
 *  image) keeps the values stable without moving the boundary meaningfully. */
export function answerCircleStyle(answer: AnswerCircle): {
  left: string;
  top: string;
  width: string;
  height: string;
} {
  const pct = (n: number) => `${Math.round(n * 10000) / 100}%`;
  const { x, y, r } = answer;
  return {
    left: pct(x - r),
    top: pct(y - r),
    width: pct(2 * r),
    height: pct(2 * r),
  };
}

/** Mirror of the game's hit test (ticket #1209): a normalized pointer is a
 *  hit when its Euclidean distance to the center is at most r (scoring.ts
 *  `isHit`). Tests pin the drawn region against this, so an overlay that
 *  drifts would fail instead of misleading Evan. */
export function answerContainsPoint(
  answer: AnswerCircle,
  x: number,
  y: number,
): boolean {
  return Math.hypot(x - answer.x, y - answer.y) <= answer.r;
}

/** Whether a payload value is a usable answer circle. The API carries the
 *  field loosely (Go `map[string]any`), so a scene without answer data or
 *  with a malformed one must render no overlay rather than a stray one. */
export function isAnswerCircle(value: unknown): value is AnswerCircle {
  if (typeof value !== "object" || value === null) return false;
  const { x, y, r } = value as Record<string, unknown>;
  if (![x, y, r].every((n) => typeof n === "number" && Number.isFinite(n))) {
    return false;
  }
  return (r as number) > 0;
}

/** The click area as a positioned overlay. Renders nothing without a valid
 *  answer, when hidden, or on an image whose frame is not the full scene
 *  (the audit crop). Decorative: the geometry is display-only, never a hit
 *  target, hence pointer-events: none + aria-hidden. */
export function AnswerOverlay({
  answer,
  visible,
}: {
  answer: unknown;
  visible: boolean;
}) {
  if (!visible || !isAnswerCircle(answer)) return null;
  return (
    <div
      className="td-answer-overlay"
      style={answerCircleStyle(answer)}
      data-testid="td-answer-overlay"
      aria-hidden="true"
    />
  );
}
