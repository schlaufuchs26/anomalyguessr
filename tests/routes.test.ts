/**
 * Pure URL routing (ticket #1223): the fragment -> mode mapping, the inverse,
 * and the prod fallback that drops a #moderation link on a host without the
 * dev flag. The integration behavior (deep links, Back/Forward, URL updates)
 * lives in tests/app.test.tsx.
 */

import { describe, expect, test } from "bun:test";
import { effectiveMode, modeFromHash, modeHash } from "../src/routes";

describe("mode fragments (#1223)", () => {
  test("a fragment names a mode; anything else is the frontpage", () => {
    expect(modeFromHash("#daily")).toBe("daily");
    expect(modeFromHash("#moderation")).toBe("moderation");
    expect(modeFromHash("#Daily")).toBe("daily");
    expect(modeFromHash("")).toBeNull();
    expect(modeFromHash("#")).toBeNull();
    expect(modeFromHash("#gallery")).toBeNull();
  });

  test("a mode maps back to its fragment; the frontpage is empty", () => {
    expect(modeHash("daily")).toBe("#daily");
    expect(modeHash("moderation")).toBe("#moderation");
    expect(modeHash(null)).toBe("");
  });

  test("moderation degrades to the frontpage without the dev flag", () => {
    expect(effectiveMode("moderation", true)).toBe("moderation");
    expect(effectiveMode("moderation", false)).toBeNull();
    expect(effectiveMode("daily", false)).toBe("daily");
    expect(effectiveMode(null, true)).toBeNull();
  });
});
