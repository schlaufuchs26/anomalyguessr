import { describe, expect, test } from "bun:test";
import { pickDay } from "../src/daily.ts";
import { mkEntry, orderTagged, type Spec } from "./dailyFixture.ts";

/**
 * The #1542 tag rule in the daily pick: the day prefers to carry one scene
 * Evan marked "funny" and one "great", swapped into the last untagged pick
 * after the variety passes. Mirrors pipeline/test_ag_queue.py's TaggedDayTest
 * and the same rule in pipeline/ag_queue.py, so a divergence between the two
 * copies is caught. (The Go copy was deleted in #1242.)
 */

describe("daily order tags (TaggedDayTest #1542)", () => {
  // The day prefers to carry one scene Evan marked "funny" and one "great";
  // both Python and TypeScript apply the same rule (ag_queue.py's
  // _place_tags), so these cases mirror pipeline/test_ag_queue.py.
  const six: Spec[] = [
    { id: "f1", added: "2026-09-01", anomaly: "Bottle" },
    { id: "f2", added: "2026-09-02", anomaly: "Can" },
    { id: "f3", added: "2026-09-03", anomaly: "Robot" },
    { id: "f4", added: "2026-09-04", anomaly: "Statue" },
    { id: "f5", added: "2026-09-05", anomaly: "Suitcase" },
    { id: "t1", added: "2026-09-06", anomaly: "Watch" },
  ];

  test("tagged scene far back lands in the day", async () => {
    // The five distinct labels fill the day first; the tagged scene takes
    // the last slot and the other four keep their labels.
    expect(await orderTagged(six, { funny: ["t1"] })).toEqual([
      "f1",
      "f2",
      "f3",
      "f4",
      "t1",
      "f5",
    ]);
  });

  test("no tagged scene leaves the day alone", async () => {
    expect(await orderTagged(six, {})).toEqual([
      "f1",
      "f2",
      "f3",
      "f4",
      "f5",
      "t1",
    ]);
  });

  test("tagged scene already picked is not swapped", async () => {
    const specs: Spec[] = [
      { id: "t1", added: "2026-09-01", anomaly: "Bottle" },
      { id: "f2", added: "2026-09-02", anomaly: "Can" },
      { id: "f3", added: "2026-09-03", anomaly: "Robot" },
      { id: "f4", added: "2026-09-04", anomaly: "Statue" },
      { id: "f5", added: "2026-09-05", anomaly: "Suitcase" },
      { id: "f6", added: "2026-09-06", anomaly: "Watch" },
    ];
    expect(await orderTagged(specs, { funny: ["t1"] })).toEqual([
      "t1",
      "f2",
      "f3",
      "f4",
      "f5",
      "f6",
    ]);
  });

  test("recycled tagged scene lands in the day", async () => {
    const specs: Spec[] = [
      { id: "f1", added: "2026-09-01", anomaly: "Bottle" },
      { id: "f2", added: "2026-09-02", anomaly: "Can" },
      { id: "f3", added: "2026-09-03", anomaly: "Robot" },
      { id: "f4", added: "2026-09-04", anomaly: "Statue" },
      { id: "f5", added: "2026-09-05", anomaly: "Suitcase" },
      {
        id: "t1",
        added: "2026-08-01",
        shown: "2026-08-10",
        anomaly: "Watch",
      },
    ];
    expect(await orderTagged(specs, { funny: ["t1"] })).toEqual([
      "f1",
      "f2",
      "f3",
      "f4",
      "t1",
      "f5",
    ]);
  });

  test("swap prefers a pick whose label repeats", () => {
    // The funny scene takes the last untagged pick; the great scene then has
    // a "Bottle" that repeats and a lone "Can" to choose from and takes the
    // repeat, so the day keeps three distinct labels.
    const candidates = [
      mkEntry({ id: "a1", added: "2026-09-01", anomaly: "Bottle" }),
      mkEntry({ id: "b1", added: "2026-09-02", anomaly: "Can" }),
      mkEntry({ id: "c1", added: "2026-09-03", anomaly: "Robot" }),
      mkEntry({ id: "a2", added: "2026-09-04", anomaly: "Bottle" }),
      mkEntry({ id: "g1", added: "2026-09-05", anomaly: "Statue" }),
    ];
    const day = pickDay(candidates, 3, {
      funny: new Set(["a2"]),
      great: new Set(["g1"]),
    });
    expect(day.map((e) => e.id)).toEqual(["g1", "b1", "a2"]);
  });
});
