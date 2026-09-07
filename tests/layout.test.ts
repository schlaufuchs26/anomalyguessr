import { describe, expect, test } from "bun:test";
import { fitInto } from "../layout";

describe("fitInto", () => {
  test("returns null when any dimension is non-positive", () => {
    expect(fitInto(0, 100, 100, 100)).toBeNull();
    expect(fitInto(100, 0, 100, 100)).toBeNull();
    expect(fitInto(100, 100, 0, 100)).toBeNull();
    expect(fitInto(100, 100, 100, 0)).toBeNull();
    expect(fitInto(-5, 100, 100, 100)).toBeNull();
  });

  test("keeps the natural size when the box is larger", () => {
    expect(fitInto(2000, 2000, 1200, 800)).toEqual({
      width: 1200,
      height: 800,
    });
  });

  test("scales down when the box width is the binding constraint", () => {
    // 1200x800 into 600x1000: width binds, scale = 0.5
    expect(fitInto(600, 1000, 1200, 800)).toEqual({ width: 600, height: 400 });
  });

  test("scales down when the box height is the binding constraint", () => {
    // 1200x800 into 2000x400: height binds, scale = 0.5
    expect(fitInto(2000, 400, 1200, 800)).toEqual({ width: 600, height: 400 });
  });

  test("never upscales a smaller image to fill the box", () => {
    expect(fitInto(300, 200, 200, 100)).toEqual({ width: 200, height: 100 });
  });

  test("rounds to whole pixels", () => {
    const fit = fitInto(100, 100, 300, 200);
    expect(fit).not.toBeNull();
    expect(Number.isInteger(fit?.width)).toBe(true);
    expect(Number.isInteger(fit?.height)).toBe(true);
    // aspect ratio preserved within rounding tolerance
    expect((fit?.width ?? 0) / (fit?.height ?? 1)).toBeCloseTo(1.5, 1);
  });
});
