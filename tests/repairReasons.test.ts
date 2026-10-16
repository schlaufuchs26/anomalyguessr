/**
 * Unit test for the repairable repair reasons (ticket #2658). The list is the
 * frontend's mirror of `pipeline/ag_repair.py`, which derives REPAIRABLE_REASONS
 * from CLICK_REASONS + TEXT_REASONS. The exact strings are pinned here and read
 * back from the Python tuples, so a one-sided rename fails the suite; the same
 * mirror check guards the canonical rejection reasons (#1627).
 */
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { REJECT_REASONS } from "../rejectReasons";
import {
  canRequestRepair,
  isRepairableReason,
  REPAIRABLE_REASONS,
} from "../repairReasons";

/** One `NAME = (...)` tuple of string literals from pipeline/ag_repair.py. */
function pythonTuple(name: string): string[] {
  const src = readFileSync(
    new URL("../pipeline/ag_repair.py", import.meta.url),
    "utf8",
  );
  const block = src.match(new RegExp(`${name} = \\(([\\s\\S]*?)\\)`));
  if (!block) throw new Error(`pipeline/ag_repair.py has no ${name} tuple`);
  return [...block[1].matchAll(/"([^"]*)"/g)].map((m) => m[1] ?? "");
}

describe("repairable repair reasons (#2658)", () => {
  test("the three repairable labels are the pinned strings", () => {
    expect(REPAIRABLE_REASONS).toEqual([
      "click area doesn't cover anomaly",
      "click area too big",
      "text is inaccurate",
    ]);
    expect(REPAIRABLE_REASONS).toHaveLength(3);
  });

  test("the Python mirror lists CLICK_REASONS + TEXT_REASONS", () => {
    expect(pythonTuple("CLICK_REASONS")).toEqual([
      "click area doesn't cover anomaly",
      "click area too big",
    ]);
    expect(pythonTuple("TEXT_REASONS")).toEqual(["text is inaccurate"]);
    expect([...REPAIRABLE_REASONS]).toEqual([
      ...pythonTuple("CLICK_REASONS"),
      ...pythonTuple("TEXT_REASONS"),
    ]);
  });

  test("every repairable reason is a canonical rejection reason", () => {
    for (const reason of REPAIRABLE_REASONS) {
      expect(REJECT_REASONS).toContain(reason);
      expect(isRepairableReason(reason)).toBe(true);
    }
    expect(isRepairableReason("too easy")).toBe(false);
    expect(isRepairableReason("")).toBe(false);
  });
});

describe("canRequestRepair (#2658)", () => {
  test("no reason keeps the button off", () => {
    expect(canRequestRepair([])).toBe(false);
  });

  test("only repairable reasons turn it on", () => {
    expect(canRequestRepair(["click area too big"])).toBe(true);
    expect(canRequestRepair(["text is inaccurate"])).toBe(true);
    expect(canRequestRepair(["click area too big", "text is inaccurate"])).toBe(
      true,
    );
  });

  test("one unrepairable reason makes the whole set unrepairable", () => {
    expect(canRequestRepair(["too easy"])).toBe(false);
    expect(canRequestRepair(["click area too big", "too easy"])).toBe(false);
  });
});
