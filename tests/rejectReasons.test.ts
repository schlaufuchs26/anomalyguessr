/**
 * Unit test for the canonical rejection reasons (ticket #1627). The list is
 * shared with the API (`api/src/routes.ts` imports it) and mirrored in the
 * Python reader (`pipeline/ag_queue.py` REJECT_REASONS), so the exact strings
 * are pinned here: renaming one would silently split the stored history.
 */
import { describe, expect, test } from "bun:test";
import { isRejectReason, REJECT_REASONS } from "../rejectReasons";

describe("canonical rejection reasons (#1627)", () => {
  test("the five labels are the pinned strings", () => {
    expect(REJECT_REASONS).toEqual([
      "doesn't match style of image",
      "click area doesn't cover anomaly",
      "scaling of anomaly is wrong",
      "anomaly doesn't make sense in context of image",
      "too easy",
    ]);
  });

  test("isRejectReason accepts only the canonical list", () => {
    for (const reason of REJECT_REASONS) {
      expect(isRejectReason(reason)).toBe(true);
    }
    expect(isRejectReason("something else")).toBe(false);
    expect(isRejectReason("")).toBe(false);
  });
});
