import { describe, expect, test } from "bun:test";
import {
  baseScore,
  bearingTo,
  clickDistance,
  isHit,
  MISS_PENALTY,
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

describe("isHit", () => {
  test("inside or exactly on the answer circle is a hit", () => {
    expect(isHit(0, 0.05)).toBe(true);
    expect(isHit(0.03, 0.05)).toBe(true);
    expect(isHit(0.05, 0.05)).toBe(true);
  });

  test("outside the answer circle is not a hit", () => {
    expect(isHit(0.051, 0.05)).toBe(false);
    expect(isHit(0.3, 0.05)).toBe(false);
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

describe("scoreFor (#1407 miss penalty)", () => {
  test("a first-try hit is exactly 100", () => {
    expect(scoreFor(0.01, 0.04, 0)).toBe(100);
    expect(scoreFor(0.04, 0.04, 0)).toBe(100);
  });

  test("each miss subtracts the flat penalty", () => {
    expect(scoreFor(0.01, 0.04, 1)).toBe(100 - MISS_PENALTY);
    expect(scoreFor(0.01, 0.04, 3)).toBe(100 - 3 * MISS_PENALTY);
  });

  test("the score never drops below zero", () => {
    expect(scoreFor(0.01, 0.04, 50)).toBe(0);
    expect(scoreFor(1.2, 0.04, 100)).toBe(0);
  });

  test("outside the radius the base is the distance score minus the penalty", () => {
    // distance 0.3 -> base 50; two misses cost 20
    expect(scoreFor(0.3, 0.04, 2)).toBe(30);
  });
});

describe("bearingTo (#1407 miss cue)", () => {
  test("east is 0, south is +pi/2 (screen coordinates)", () => {
    expect(bearingTo({ x: 0.5, y: 0.5 }, { x: 0.8, y: 0.5 })).toBeCloseTo(0);
    expect(bearingTo({ x: 0.5, y: 0.5 }, { x: 0.5, y: 0.9 })).toBeCloseTo(
      Math.PI / 2,
    );
  });

  test("north-west is -3/4 pi", () => {
    expect(bearingTo({ x: 0.5, y: 0.5 }, { x: 0.2, y: 0.2 })).toBeCloseTo(
      -Math.PI * 0.75,
    );
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
