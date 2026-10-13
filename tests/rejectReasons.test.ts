/**
 * Unit test for the canonical rejection reasons (ticket #1627). The list is
 * shared with the API (`api/src/routes.ts` imports it) and mirrored in the
 * Python reader (`pipeline/ag_queue.py` REJECT_REASONS), so the exact strings
 * are pinned here: renaming one would silently split the stored history.
 * Since #2585 a decision can carry several reasons, so `entryReasons` reads
 * both the legacy single string and the new list.
 */
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { entryReasons, isRejectReason, REJECT_REASONS } from "../rejectReasons";

/** The REJECT_REASONS tuple as written in the Python mirror. */
function pythonMirror(): string[] {
  const src = readFileSync(
    new URL("../pipeline/ag_queue.py", import.meta.url),
    "utf8",
  );
  const block = src.match(/REJECT_REASONS = \(([\s\S]*?)\)/);
  if (!block)
    throw new Error("pipeline/ag_queue.py has no REJECT_REASONS tuple");
  return [...block[1].matchAll(/"([^"]*)"/g)].map((m) => m[1] ?? "");
}

describe("canonical rejection reasons (#1627)", () => {
  test("the eight labels are the pinned strings", () => {
    expect(REJECT_REASONS).toEqual([
      "doesn't match style of image",
      "click area doesn't cover anomaly",
      "click area too big",
      "scaling of anomaly is wrong",
      "anomaly doesn't make sense in context of image",
      "too easy",
      "too hard",
      "text is inaccurate",
    ]);
    expect(REJECT_REASONS).toHaveLength(8);
  });

  test("isRejectReason accepts only the canonical list", () => {
    for (const reason of REJECT_REASONS) {
      expect(isRejectReason(reason)).toBe(true);
    }
    expect(isRejectReason("something else")).toBe(false);
    expect(isRejectReason("")).toBe(false);
  });

  test("the Python mirror lists the same reasons (#1888)", () => {
    expect(pythonMirror()).toEqual([...REJECT_REASONS]);
  });
});

describe("stored reason entries (#2585)", () => {
  test("a legacy single string reads as a one-element list", () => {
    expect(entryReasons({ reason: "too easy", at: "x" })).toEqual(["too easy"]);
  });

  test("a new list reads unchanged", () => {
    expect(entryReasons({ reason: ["too easy", "too hard"], at: "x" })).toEqual(
      ["too easy", "too hard"],
    );
  });

  test("missing, empty and unknown reasons read as an empty list", () => {
    expect(entryReasons(undefined)).toEqual([]);
    expect(entryReasons(null)).toEqual([]);
    expect(entryReasons({ at: "x" })).toEqual([]);
    expect(entryReasons({ reason: "my own wording", at: "x" })).toEqual([]);
  });
});
