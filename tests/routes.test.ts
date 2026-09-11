/**
 * Pure path routing (ticket #1223, reworked): path -> mode, mode -> path and
 * the prod fallback that drops `/moderation` on a host without the dev flag.
 * The integration behavior (deep links, Back/Forward, pushState, the 404
 * shim) lives in tests/app.test.tsx.
 */

import { describe, expect, test } from "bun:test";
import { effectiveMode, modeFromPath, modePath } from "../src/routes";

const ROOT = new URL("https://anomalyguessr.com/");
const DEV = new URL("https://fuchs.science/anomalyguessr/");

describe("mode paths (#1223)", () => {
  test("a mode resolves from its path under either base", () => {
    expect(modeFromPath("/daily", ROOT)).toBe("daily");
    expect(modeFromPath("/moderation", ROOT)).toBe("moderation");
    expect(modeFromPath("/anomalyguessr/daily", DEV)).toBe("daily");
    expect(modeFromPath("/anomalyguessr/moderation", DEV)).toBe("moderation");
  });

  test("the base, unknown and out-of-base paths are the frontpage", () => {
    expect(modeFromPath("/", ROOT)).toBeNull();
    expect(modeFromPath("/anomalyguessr/", DEV)).toBeNull();
    expect(modeFromPath("/gallery/", ROOT)).toBeNull();
    expect(modeFromPath("/unknown", ROOT)).toBeNull();
    // trailing slash is not a mode path (single segment, no slash)
    expect(modeFromPath("/daily/", ROOT)).toBeNull();
    // a mode path of another base is not ours
    expect(modeFromPath("/anomalyguessr/daily", ROOT)).toBeNull();
    expect(modeFromPath("/daily", DEV)).toBeNull();
  });

  test("a mode maps back to the path under its base", () => {
    expect(modePath(ROOT, null)).toBe("/");
    expect(modePath(ROOT, "daily")).toBe("/daily");
    expect(modePath(DEV, null)).toBe("/anomalyguessr/");
    expect(modePath(DEV, "moderation")).toBe("/anomalyguessr/moderation");
  });

  test("moderation degrades to the frontpage without the dev flag", () => {
    expect(effectiveMode("moderation", true)).toBe("moderation");
    expect(effectiveMode("moderation", false)).toBeNull();
    expect(effectiveMode("daily", false)).toBe("daily");
    expect(effectiveMode(null, true)).toBeNull();
  });
});
