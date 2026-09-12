import { describe, expect, test } from "bun:test";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { DAILY_COUNT, dailyOrder, pickDay } from "../src/daily.ts";
import { type Env, handle } from "../src/routes.ts";
import type { Manifest, SceneList } from "../src/scenes.ts";
import { Store } from "../src/store.ts";
import type { SceneEntry, StateFile } from "../src/types.ts";
import { testEnv } from "./helpers.ts";

/**
 * The daily pick rule is one rule with one origin
 * (pipeline/ag_queue.py's day_candidates/choose_day/daily_order); this suite
 * mirrors the fixture cases of pipeline/test_ag_queue.py (RecycleTest #1206,
 * AnomalyVarietyTest #1229, FamilyVarietyTest #1232), so a divergence in this
 * TypeScript copy is caught. (The Go copy and its TestTDDailyOrder* tests
 * were deleted in #1242.)
 */

interface Spec {
  id: string;
  added: string;
  shown?: string;
  anomaly?: string;
  family?: string;
  /** Legacy pre-#1136 entry: cannot be a version-2 manifest scene. */
  noSource?: boolean;
}

function mkEntry(spec: Spec): SceneEntry {
  const e: SceneEntry = {
    id: spec.id,
    title: spec.id,
    place: "P",
    year: "1900",
    credit: "PD",
    sourceUrl: `https://example.test/${spec.id}`,
    anomaly: spec.anomaly ?? "Object",
    description: "d",
    answer: { x: 0.5, y: 0.5, r: 0.05 },
    hints: ["a", "b", "c"],
    added: spec.added,
    shown: spec.shown ?? null,
  };
  if (!spec.noSource) e.source = { repository: "R", license: "PD" };
  if (spec.family !== undefined) e.family = spec.family;
  return e;
}

// Writes a state.json/feedback.json pair; `accepted` undefined accepts every
// scene (pass [] to accept none), mirroring the Go/TS fixture helpers.
async function fixture(
  specs: Spec[],
  accepted?: string[],
  rejected: string[] = [],
): Promise<{ env: Env; dir: string }> {
  const dir = await mkdtemp(path.join(tmpdir(), "ag-daily-"));
  const scenes: Record<string, SceneEntry> = {};
  for (const s of specs) scenes[s.id] = mkEntry(s);
  const state: StateFile = { version: 1, last_shipped: null, scenes };
  await writeFile(
    path.join(dir, "state.json"),
    `${JSON.stringify(state, null, 2)}\n`,
  );
  const acc: Record<string, string> = {};
  const rej: Record<string, string> = {};
  for (const id of accepted ?? specs.map((s) => s.id)) {
    acc[id] = "2026-09-01T00:00:00Z";
  }
  for (const id of rejected) rej[id] = "2026-09-01T00:00:00Z";
  await writeFile(
    path.join(dir, "feedback.json"),
    `${JSON.stringify(
      { version: 1, accepted: acc, rejected: rej, comments: {} },
      null,
      2,
    )}\n`,
  );
  // The daily-order tests never touch /generate.
  return { env: testEnv(new Store(dir), dir), dir };
}

async function order(
  specs: Spec[],
  accepted?: string[],
  rejected: string[] = [],
): Promise<string[]> {
  const { env } = await fixture(specs, accepted, rejected);
  return dailyOrder(await env.store.state(), await env.store.feedback());
}

function req(method: string, url: string): Request {
  return new Request(`http://test.local/anomalyguessr/api/${url}`, { method });
}

async function json<T>(res: Response): Promise<T> {
  return (await res.json()) as T;
}

describe("daily order (RecycleTest #1206)", () => {
  const cases: {
    name: string;
    specs: Spec[];
    accepted?: string[];
    rejected?: string[];
    want: string[];
  }[] = [
    {
      name: "fresh_preferred_over_recycled",
      specs: [
        { id: "r1", added: "2026-08-01", shown: "2026-08-05" },
        { id: "r2", added: "2026-08-02", shown: "2026-08-06" },
        { id: "r3", added: "2026-08-03", shown: "2026-08-07" },
        { id: "f1", added: "2026-09-01" },
        { id: "f2", added: "2026-09-02" },
        { id: "f3", added: "2026-09-03" },
      ],
      want: ["f1", "f2", "f3", "r1", "r2", "r3"],
    },
    {
      name: "lru_oldest_shown_first",
      specs: [
        { id: "s1", added: "2026-08-01", shown: "2026-08-10" },
        { id: "s2", added: "2026-08-01", shown: "2026-08-11" },
        { id: "s3", added: "2026-08-01", shown: "2026-08-12" },
        { id: "s4", added: "2026-08-01", shown: "2026-08-13" },
        { id: "s5", added: "2026-08-01", shown: "2026-08-14" },
        { id: "s6", added: "2026-08-01", shown: "2026-08-15" },
      ],
      want: ["s1", "s2", "s3", "s4", "s5", "s6"],
    },
    {
      name: "only_shown_scenes_do_not_error",
      specs: [
        { id: "s1", added: "2026-08-01", shown: "2026-08-10" },
        { id: "s2", added: "2026-08-01", shown: "2026-08-11" },
        { id: "s3", added: "2026-08-01", shown: "2026-08-12" },
      ],
      want: ["s1", "s2", "s3"],
    },
    {
      name: "no_duplicate_within_one_day",
      specs: [
        { id: "r1", added: "2026-08-01", shown: "2026-08-05" },
        { id: "r2", added: "2026-08-02", shown: "2026-08-06" },
        { id: "f1", added: "2026-09-01" },
      ],
      want: ["f1", "r1", "r2"],
    },
    {
      name: "rejected_and_unmoderated_not_recycled",
      specs: [
        { id: "r1", added: "2026-08-01", shown: "2026-08-05" },
        { id: "r2", added: "2026-08-02", shown: "2026-08-06" },
        { id: "r3", added: "2026-08-03", shown: "2026-08-07" },
      ],
      accepted: ["r1", "r2"],
      rejected: ["r1"],
      want: ["r2"],
    },
    {
      name: "unmoderated_fresh_scene_not_picked",
      specs: [
        { id: "a1", added: "2026-09-01" },
        { id: "a2", added: "2026-09-02" },
      ],
      accepted: ["a1"],
      want: ["a1"],
    },
    {
      name: "empty_accepted_pool_is_empty_order",
      specs: [{ id: "u1", added: "2026-08-01" }],
      accepted: [],
      want: [],
    },
    {
      name: "same_day_tiebreak_by_id",
      specs: [
        { id: "bbb", added: "2026-09-01" },
        { id: "aaa", added: "2026-09-01" },
        { id: "ccc", added: "2026-09-01", shown: "2026-08-01" },
        { id: "ddd", added: "2026-09-01", shown: "2026-08-01" },
      ],
      want: ["aaa", "bbb", "ccc", "ddd"],
    },
  ];
  for (const c of cases) {
    test(c.name, async () => {
      expect(await order(c.specs, c.accepted, c.rejected)).toEqual(c.want);
    });
  }
});

describe("daily order variety (AnomalyVarietyTest #1229)", () => {
  const cases: { name: string; specs: Spec[]; want: string[] }[] = [
    {
      name: "distinct_labels_preferred_over_repeats",
      specs: [
        { id: "b1", added: "2026-09-01", anomaly: "Bottle" },
        { id: "b2", added: "2026-09-02", anomaly: "Bottle" },
        { id: "b3", added: "2026-09-03", anomaly: "Bottle" },
        { id: "c1", added: "2026-09-04", anomaly: "Can" },
        { id: "r1", added: "2026-09-05", anomaly: "Robot" },
        { id: "b4", added: "2026-09-06", anomaly: "Bottle" },
      ],
      want: ["b1", "c1", "r1", "b2", "b3", "b4"],
    },
    {
      name: "fewer_than_five_distinct_labels_still_fills",
      specs: [
        { id: "a1", added: "2026-09-01", anomaly: "Bottle" },
        { id: "a2", added: "2026-09-02", anomaly: "Can" },
        { id: "a3", added: "2026-09-03", anomaly: "Bottle" },
        { id: "a4", added: "2026-09-04", anomaly: "Can" },
        { id: "a5", added: "2026-09-05", anomaly: "Bottle" },
        { id: "a6", added: "2026-09-06", anomaly: "Can" },
      ],
      want: ["a1", "a2", "a3", "a4", "a5", "a6"],
    },
    {
      name: "all_distinct_labels_keep_freshness_order",
      specs: [
        { id: "f1", added: "2026-09-01", anomaly: "Label 1" },
        { id: "f2", added: "2026-09-02", anomaly: "Label 2" },
        { id: "f3", added: "2026-09-03", anomaly: "Label 3" },
        { id: "f4", added: "2026-09-04", anomaly: "Label 4" },
        { id: "f5", added: "2026-09-05", anomaly: "Label 5" },
        { id: "f6", added: "2026-09-06", anomaly: "Label 6" },
        { id: "f7", added: "2026-09-07", anomaly: "Label 7" },
      ],
      want: ["f1", "f2", "f3", "f4", "f5", "f6", "f7"],
    },
    {
      name: "variety_reaches_into_the_back_catalogue",
      specs: [
        { id: "f1", added: "2026-09-01", anomaly: "Bottle" },
        { id: "f2", added: "2026-09-02", anomaly: "Bottle" },
        { id: "f3", added: "2026-09-03", anomaly: "Bottle" },
        { id: "r1", added: "2026-08-01", shown: "2026-08-10", anomaly: "Can" },
        {
          id: "r2",
          added: "2026-08-01",
          shown: "2026-08-11",
          anomaly: "Robot",
        },
        {
          id: "r3",
          added: "2026-08-01",
          shown: "2026-08-12",
          anomaly: "Statue",
        },
      ],
      want: ["f1", "r1", "r2", "r3", "f2", "f3"],
    },
  ];
  for (const c of cases) {
    test(c.name, async () => {
      expect(await order(c.specs)).toEqual(c.want);
    });
  }
});

describe("daily order family variety (FamilyVarietyTest #1232)", () => {
  const cases: { name: string; specs: Spec[]; want: string[] }[] = [
    {
      name: "family_duplicate_loses_its_slot_to_a_recycled_scene",
      specs: [
        {
          id: "f1",
          added: "2026-09-01",
          anomaly: "Plastic bottle (clear PET)",
          family: "drinks",
        },
        {
          id: "f2",
          added: "2026-09-02",
          anomaly: "Plastic water bottle (PET)",
          family: "drinks",
        },
        {
          id: "f3",
          added: "2026-09-03",
          anomaly: "Plastic bag (white, with handles)",
          family: "plastic",
        },
        {
          id: "f4",
          added: "2026-09-04",
          anomaly: "E-scooter",
          family: "vehicle",
        },
        {
          id: "f5",
          added: "2026-09-05",
          anomaly: "Over-ear headphones",
          family: "electronics",
        },
        {
          id: "r1",
          added: "2026-08-01",
          shown: "2026-08-10",
          anomaly: "Wheeled suitcase",
          family: "luggage",
        },
      ],
      want: ["f1", "f3", "f4", "f5", "r1", "f2"],
    },
    {
      name: "same_family_still_fills_when_no_new_family_is_left",
      specs: [
        { id: "c1", added: "2026-09-01", anomaly: "Drink 1", family: "drinks" },
        { id: "c2", added: "2026-09-02", anomaly: "Drink 2", family: "drinks" },
        { id: "c3", added: "2026-09-03", anomaly: "Drink 3", family: "drinks" },
        { id: "c4", added: "2026-09-04", anomaly: "Drink 4", family: "drinks" },
        { id: "c5", added: "2026-09-05", anomaly: "Drink 5", family: "drinks" },
      ],
      want: ["c1", "c2", "c3", "c4", "c5"],
    },
  ];
  for (const c of cases) {
    test(c.name, async () => {
      expect(await order(c.specs)).toEqual(c.want);
    });
  }

  test("second pass takes a new-label sibling before the exact label repeat", () => {
    const candidates = [
      mkEntry({
        id: "a1",
        added: "2026-09-01",
        anomaly: "Can (matte aluminium)",
        family: "drinks",
      }),
      mkEntry({
        id: "a2",
        added: "2026-09-02",
        anomaly: "Pepsi can",
        family: "drinks",
      }),
      mkEntry({
        id: "a3",
        added: "2026-09-03",
        anomaly: "Can (matte aluminium)",
        family: "drinks",
      }),
    ];
    expect(pickDay(candidates, 2).map((e) => e.id)).toEqual(["a1", "a2"]);
  });
});

describe("API wiring", () => {
  test("GET scenes carries dailyOrder + dailyCount, unmoderated still listed", async () => {
    const { env } = await fixture(
      [
        { id: "r1", added: "2026-08-01", shown: "2026-08-05" },
        { id: "f1", added: "2026-09-01" },
        { id: "f2", added: "2026-09-02" },
        { id: "u1", added: "2026-09-03" },
      ],
      ["r1", "f1", "f2"],
    );
    const list = await json<SceneList>(
      await handle(env, "GET", "scenes", req("GET", "scenes")),
    );
    expect(list.dailyCount).toBe(5);
    expect(list.dailyOrder).toEqual(["f1", "f2", "r1"]);
    expect(list.scenes.length).toBe(4);
  });

  test("manifest scope=daily is the day's set in ship order", async () => {
    const { env } = await fixture([
      { id: "r1", added: "2026-08-01", shown: "2026-08-05" },
      { id: "f1", added: "2026-09-01" },
      { id: "f2", added: "2026-09-02" },
      { id: "f3", added: "2026-09-03" },
      { id: "f4", added: "2026-09-04" },
      { id: "f5", added: "2026-09-05" },
    ]);
    const res = await handle(
      env,
      "GET",
      "manifest",
      req("GET", "manifest?scope=daily"),
    );
    expect(res.status).toBe(200);
    const body = await json<Manifest>(res);
    expect(body.scope).toBe("daily");
    expect(body.moderation).toBe(true);
    expect(body.scenes.map((s) => s.id)).toEqual([
      "f1",
      "f2",
      "f3",
      "f4",
      "f5",
    ]);
    expect(body.scenes[0]?.image).toBe("/anomalyguessr/api/scenes/f1/image");
  });

  test("manifest scope=daily skips sourceless legacy entries", async () => {
    const { env } = await fixture([
      { id: "f1", added: "2026-09-01" },
      { id: "f2", added: "2026-09-02" },
      { id: "f3", added: "2026-09-03", noSource: true },
      { id: "f4", added: "2026-09-04" },
      { id: "f5", added: "2026-09-05" },
      { id: "f6", added: "2026-09-06" },
    ]);
    const body = await json<Manifest>(
      await handle(env, "GET", "manifest", req("GET", "manifest?scope=daily")),
    );
    expect(body.scenes.map((s) => s.id)).toEqual([
      "f1",
      "f2",
      "f4",
      "f5",
      "f6",
    ]);
    expect(DAILY_COUNT).toBe(5);
  });

  test("manifest rejects an unknown scope", async () => {
    const { env } = await fixture([{ id: "f1", added: "2026-09-01" }]);
    const res = await handle(
      env,
      "GET",
      "manifest",
      req("GET", "manifest?scope=bogus"),
    );
    expect(res.status).toBe(400);
  });
});
