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

/** Score multipliers after using 0..3 hints. */
export const HINT_MULTIPLIERS = [1, 0.85, 0.7, 0.55] as const;

/**
 * Final score for a click: perfect hits (within the answer radius) score 100
 * before the hint multiplier; everything else is distance-based.
 */
export function scoreFor(
  distance: number,
  radius: number,
  hintsUsed: number,
): number {
  const base = distance <= radius ? 100 : baseScore(distance);
  const multiplier =
    HINT_MULTIPLIERS[Math.min(hintsUsed, HINT_MULTIPLIERS.length - 1)] ?? 1;
  return Math.max(0, Math.round(base * multiplier));
}

export type Verdict = "saved" | "warm" | "miss";

export function verdictFor(score: number): Verdict {
  if (score >= 90) return "saved";
  if (score >= 60) return "warm";
  return "miss";
}
