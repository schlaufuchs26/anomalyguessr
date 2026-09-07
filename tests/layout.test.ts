import { describe, expect, test } from "bun:test";
import {
  clampView,
  fitInto,
  panBy,
  VIEW_MAX_SCALE,
  VIEW_MIN_SCALE,
  zoomAt,
} from "../layout";

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

describe("clampView", () => {
  test("identity at scale 1, no pan", () => {
    expect(clampView(0, 0, 1, 800, 600)).toEqual({ scale: 1, x: 0, y: 0 });
  });

  test("clamps pan so the image cannot leave the view", () => {
    const v = clampView(9999, -9999, 3, 800, 600);
    expect(v.x).toBe(0); // right edge can come to x=0 at most... clamp keeps x<=0
    expect(v.y).toBeLessThanOrEqual(0);
    // when zoomed to 3x in an 800-wide view, x must be >= 800-2400
    expect(v.x).toBeGreaterThanOrEqual(800 - 800 * 3);
  });

  test("clamps the scale to the allowed range", () => {
    expect(clampView(0, 0, 100, 800, 600).scale).toBe(VIEW_MAX_SCALE);
    expect(clampView(0, 0, 0.1, 800, 600).scale).toBe(1);
  });
});

describe("zoomAt", () => {
  const base = { scale: 1, x: 0, y: 0 };

  test("zooming in keeps the image point under the cursor fixed", () => {
    // cursor at (400, 300) on an 800x600 view -> that image point stays put
    const v = zoomAt(base, 800, 600, 400, 300, 2);
    expect(v.scale).toBe(2);
    expect(v.x).toBe(400 - (400 / 1) * 2); // 400 - 800 = -400
    expect(v.y).toBe(300 - (300 / 1) * 2); // 300 - 600 = -300
  });

  test("never zooms below 1", () => {
    expect(zoomAt(base, 800, 600, 400, 300, 0.5).scale).toBe(VIEW_MIN_SCALE);
  });

  test("zooming out to scale 1 recentres on the origin", () => {
    const v = zoomAt(base, 800, 600, 400, 300, 4);
    const out = zoomAt(v, 800, 600, 400, 300, 0.25);
    expect(out.scale).toBe(1);
    expect(out.x).toBeCloseTo(0, 5);
    expect(out.y).toBeCloseTo(0, 5);
  });
});

describe("panBy", () => {
  test("moves and clamps", () => {
    const v = panBy({ scale: 2, x: -100, y: -50 }, 30, 20, 800, 600);
    expect(v.x).toBe(-70);
    expect(v.y).toBe(-30);
  });
});
