import type { Scene } from "../manifest";
import {
  clickDistance,
  isHit,
  scoreFor,
  type Verdict,
  verdictFor,
} from "../scoring";

/**
 * A resolved guess at normalized image coordinates (0..1, top-left origin).
 * Everything the HUD renders after a guess derives from this: the score, the
 * verdict copy key and whether the original photo is revealed (#1147).
 */
export interface GuessResult {
  x: number;
  y: number;
  score: number;
  hit: boolean;
  verdict: Verdict;
}

/** Score a click against a scene's answer circle; misses cost MISS_PENALTY. */
export function resolveGuess(
  scene: Scene,
  x: number,
  y: number,
  misses: number,
): GuessResult {
  const distance = clickDistance({ x, y }, scene.answer);
  const score = scoreFor(distance, scene.answer.r, misses);
  return {
    x,
    y,
    score,
    hit: isHit(distance, scene.answer.r),
    verdict: verdictFor(score),
  };
}
