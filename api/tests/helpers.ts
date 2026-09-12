import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { type BatchRunner, Generator } from "../src/generate.ts";
import type { Env } from "../src/routes.ts";
import { Store } from "../src/store.ts";
import type { SceneEntry, StateFile } from "../src/types.ts";

/**
 * Shared fixture helpers for the API tests: a temp data dir with two scenes
 * (one shown with an audit crop, one unshown) plus the image files, mirroring
 * the old Go test tree.
 */

/** One state.json scene entry; `source: false` makes it a legacy pre-v2 entry. */
export function scene(
  id: string,
  added: string,
  shown: string | null,
  source = true,
): SceneEntry {
  return {
    id,
    title: id,
    place: "P",
    year: "1900",
    credit: "PD",
    sourceUrl: `https://example.test/${id}`,
    ...(source ? { source: { repository: "R", license: "PD" } } : {}),
    anomaly: "Plastic bottle",
    description: "d",
    answer: { x: 0.5, y: 0.5, r: 0.05 },
    hints: ["a"],
    added,
    shown,
  };
}

/** Wraps a store + data dir in the env the routes need. */
export function testEnv(
  store: Store,
  dir: string,
  runner: BatchRunner = async () => {},
): Env {
  // pipelineDir/envFile/logFile only matter to the default runner, which
  // tests always replace: no test may spend image-model money or launch
  // ag_generate.py.
  return {
    store,
    dataDir: dir,
    generator: new Generator({
      store,
      pipelineDir: dir,
      envFile: path.join(dir, ".env"),
      logFile: path.join(dir, "generate.log"),
      runner,
    }),
  };
}

/** Writes a state.json into a data dir. */
export async function writeState(dir: string, st: StateFile): Promise<void> {
  await writeFile(
    path.join(dir, "state.json"),
    `${JSON.stringify(st, null, 2)}\n`,
  );
}

export async function makeEnv(
  runner?: BatchRunner,
): Promise<{ env: Env; dir: string }> {
  const dir = await mkdtemp(path.join(tmpdir(), "ag-api-"));
  const td = path.join(dir, "data", "anomalyguessr");
  const libA = path.join(td, "library", "alpha-market");
  const libB = path.join(td, "library", "beta-street");
  await mkdir(libA, { recursive: true });
  await mkdir(libB, { recursive: true });
  await mkdir(path.join(td, "audit", "alpha-market"), { recursive: true });
  await writeFile(path.join(libA, "alpha-market.jpg"), "alpha-edited");
  await writeFile(
    path.join(libA, "alpha-market-original.jpg"),
    "alpha-original",
  );
  await writeFile(path.join(libB, "beta-street.jpg"), "beta-edited");
  await writeFile(path.join(libB, "beta-street-original.jpg"), "beta-original");
  await writeFile(
    path.join(td, "audit", "alpha-market", "hotspot-crop.png"),
    "alpha-crop",
  );

  const state: StateFile = {
    version: 1,
    last_shipped: null,
    scenes: {
      "alpha-market": {
        ...scene("alpha-market", "2026-09-07", "2026-09-07"),
        title: "Old Market",
        place: "Alpha",
        credit: "PD",
        anomaly: "Plastic bottle",
        description: "A market.",
        hints: ["a", "b", "c"],
        explanation: "plastic did not exist yet",
        references: [{ label: "ref", url: "https://example.test/r" }],
      },
      "beta-street": {
        ...scene("beta-street", "2026-09-08", null),
        title: "Beta Street",
        place: "Beta",
        anomaly: "Digital watch",
        description: "A street.",
        hints: ["d", "e", "f"],
      },
    },
  };
  await writeState(td, state);
  const store = new Store(td);
  return { env: testEnv(store, td, runner), dir: td };
}
