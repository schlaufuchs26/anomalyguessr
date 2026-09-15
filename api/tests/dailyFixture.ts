import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { dailyOrder } from "../src/daily.ts";
import type { Env } from "../src/routes.ts";
import { Store } from "../src/store.ts";
import type { SceneEntry, StateFile } from "../src/types.ts";
import { testEnv } from "./helpers.ts";

/**
 * Fixtures shared by the daily-pick suites (daily.test.ts's variety cases,
 * dailyTags.test.ts's #1542 tag cases): a temp data dir holding the
 * state.json/feedback.json pair the pipeline reads and the API writes.
 */

export interface Spec {
  id: string;
  added: string;
  shown?: string;
  anomaly?: string;
  family?: string;
  /** Legacy pre-#1136 entry: cannot be a version-2 manifest scene. */
  noSource?: boolean;
}

export function mkEntry(spec: Spec): SceneEntry {
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

/**
 * Writes a state.json/feedback.json pair; `accepted` undefined accepts every
 * scene (pass [] to accept none), mirroring the Go/TS fixture helpers. `tags`
 * are the optional "funny"/"great" moderation marks (tickets #1502/#1541).
 */
export async function fixture(
  specs: Spec[],
  accepted?: string[],
  rejected: string[] = [],
  tags: { funny?: string[]; great?: string[] } = {},
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
  const tagMap = (ids: string[] = []): Record<string, string> =>
    Object.fromEntries(ids.map((id) => [id, "2026-09-01T00:00:00Z"]));
  await writeFile(
    path.join(dir, "feedback.json"),
    `${JSON.stringify(
      {
        version: 1,
        accepted: acc,
        rejected: rej,
        comments: {},
        funny: tagMap(tags.funny),
        great: tagMap(tags.great),
      },
      null,
      2,
    )}\n`,
  );
  // The daily-order tests never touch /generate.
  return { env: testEnv(new Store(dir), dir), dir };
}

export async function order(
  specs: Spec[],
  accepted?: string[],
  rejected: string[] = [],
): Promise<string[]> {
  const { env } = await fixture(specs, accepted, rejected);
  return dailyOrder(await env.store.state(), await env.store.feedback());
}

/** `order` with the #1542 moderation tags written into feedback.json. */
export async function orderTagged(
  specs: Spec[],
  tags: { funny?: string[]; great?: string[] },
): Promise<string[]> {
  const { env } = await fixture(specs, undefined, [], tags);
  return dailyOrder(await env.store.state(), await env.store.feedback());
}
