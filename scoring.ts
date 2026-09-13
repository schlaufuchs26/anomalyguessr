export interface Point {
  x: number;
  y: number;
}

/** Euclidean distance between two normalized points (unit-square space). */
export function clickDistance(click: Point, center: Point): number {
  return Math.hypot(click.x - center.x, click.y - center.y);
}

/**
 * Whether a click counts as a correct guess: it landed inside the answer
 * circle (distance to the planted anomaly's center at most its radius).
 * This is the geometric "perfect hit" that scores 100 (ticket #1147): the
 * reveal to the original photo happens on it and only on it.
 */
export function isHit(distance: number, radius: number): boolean {
  return distance <= radius;
}

/** Base score from distance: 100 at 0 distance, 0 at 60% of the frame away. */
export function baseScore(distance: number): number {
  if (distance <= 0) return 100;
  return Math.max(0, Math.round(100 * (1 - distance / 0.6)));
}

/**
 * Flat point cost of a miss (ticket #1407). A miss is a click outside the
 * answer radius; the player keeps the scene and gets a bearing cue, so the
 * penalty is what makes extra attempts costly. Flat and not
 * distance-weighted: the far click already gets the direction cue as
 * information, and one variable keeps the rule explainable.
 */
export const MISS_PENALTY = 10;

/**
 * Final score of one scene: the base score of the resolving click (100 inside
 * the answer radius, distance-based otherwise) minus MISS_PENALTY per miss,
 * floored at 0. A first-try hit is exactly 100.
 */
export function scoreFor(
  distance: number,
  radius: number,
  misses: number,
): number {
  const base = distance <= radius ? 100 : baseScore(distance);
  return Math.max(0, Math.round(base - MISS_PENALTY * misses));
}

/**
 * Bearing from `from` toward `to` in radians, for the miss cue's arrow
 * (ticket #1407). Screen coordinates (y grows downward), so 0 points east
 * and positive angles turn clockwise; a CSS `rotate(<angle>rad)` matches.
 */
export function bearingTo(from: Point, to: Point): number {
  return Math.atan2(to.y - from.y, to.x - from.x);
}

export type Verdict = "saved" | "warm" | "miss";

export function verdictFor(score: number): Verdict {
  if (score >= 90) return "saved";
  if (score >= 60) return "warm";
  return "miss";
}
