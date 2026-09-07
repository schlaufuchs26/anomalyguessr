import { describe, expect, test } from "bun:test";
import {
  baseScore,
  clickDistance,
  HINT_MULTIPLIERS,
  scoreFor,
  verdictFor,
} from "../scoring";

describe("clickDistance", () => {
  test("zero for identical points", () => {
    expect(clickDistance({ x: 0.4, y: 0.3 }, { x: 0.4, y: 0.3 })).toBe(0);
  });

  test("euclidean in unit space", () => {
    expect(clickDistance({ x: 0, y: 0 }, { x: 0.3, y: 0.4 })).toBeCloseTo(0.5);
  });
});

describe("baseScore", () => {
  test("perfect at distance 0", () => {
    expect(baseScore(0)).toBe(100);
  });

  test("linear falloff to zero at 0.6", () => {
    expect(baseScore(0.3)).toBe(50);
    expect(baseScore(0.6)).toBe(0);
    expect(baseScore(1.2)).toBe(0);
  });
});

describe("scoreFor", () => {
  test("perfect hit within radius ignores distance", () => {
    expect(scoreFor(0.01, 0.04, 0)).toBe(100);
    expect(scoreFor(0.04, 0.04, 0)).toBe(100);
  });

  test("hint multipliers reduce the score", () => {
    const noHints = scoreFor(0.2, 0.04, 0);
    const oneHint = scoreFor(0.2, 0.04, 1);
    const mult = HINT_MULTIPLIERS[1];
    expect(oneHint).toBe(Math.round(noHints * (mult ?? 1)));
    expect(oneHint).toBeLessThan(noHints);
  });

  test("hintsUsed beyond the list clamps to the last multiplier", () => {
    expect(scoreFor(0.2, 0.04, 7)).toBe(scoreFor(0.2, 0.04, 3));
  });

  test("never negative", () => {
    expect(scoreFor(2, 0.04, 3)).toBe(0);
  });
});

describe("verdictFor", () => {
  test("bands at 90 and 60", () => {
    expect(verdictFor(100)).toBe("saved");
    expect(verdictFor(90)).toBe("saved");
    expect(verdictFor(89)).toBe("warm");
    expect(verdictFor(60)).toBe("warm");
    expect(verdictFor(59)).toBe("miss");
    expect(verdictFor(0)).toBe("miss");
  });
});
