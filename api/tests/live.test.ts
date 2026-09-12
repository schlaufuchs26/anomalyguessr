import { describe, expect, test } from "bun:test";
import { handle } from "../src/routes.ts";
import type { Manifest } from "../src/scenes.ts";
import type { StateFile } from "../src/types.ts";
import { makeEnv, scene, writeState } from "./helpers.ts";

function req(method: string, url: string): Request {
  return new Request(`http://test.local/anomalyguessr/api/${url}`, { method });
}

/** Parses a response body with the caller's declared shape. */
async function json<T>(res: Response): Promise<T> {
  return (await res.json()) as T;
}

describe("manifest scope=live (#1237)", () => {
  // The last ship: s1/s2/s3 shown on 2026-09-10, recorded in the manifest
  // order s3, s1, s2; plus a scene from an older ship, a sourceless legacy
  // entry and (later) a rejection after the ship.
  const shipped = (): StateFile => ({
    version: 1,
    last_shipped: "2026-09-10",
    last_shipped_ids: ["s3", "s1", "s2"],
    scenes: {
      s1: scene("s1", "2026-09-01", "2026-09-10"),
      s2: scene("s2", "2026-09-02", "2026-09-10"),
      s3: scene("s3", "2026-09-03", "2026-09-10"),
      older: scene("older", "2026-08-01", "2026-09-05"),
      nosrc: scene("nosrc", "2026-09-04", "2026-09-10", false),
    },
  });

  test("the shipped set in recorded order, dated last_shipped", async () => {
    const { env, dir } = await makeEnv();
    await writeState(dir, shipped());
    const body = await json<Manifest>(
      await handle(env, "GET", "manifest", req("GET", "manifest?scope=live")),
    );
    expect(body.scope).toBe("live");
    expect(body.version).toBe(2);
    // recorded order wins over added order; the older ship and the
    // sourceless legacy entry stay out (it cannot be a v2 scene)
    expect(body.scenes.map((s) => s.id)).toEqual(["s3", "s1", "s2"]);
    expect(body.date).toBe("2026-09-10");
  });

  test("a scene rejected after the ship drops out", async () => {
    const { env, dir } = await makeEnv();
    await writeState(dir, shipped());
    await handle(env, "POST", "scenes/s1/reject", req("POST", "x"));
    const body = await json<Manifest>(
      await handle(env, "GET", "manifest", req("GET", "manifest?scope=live")),
    );
    expect(body.scenes.map((s) => s.id)).toEqual(["s3", "s2"]);
  });

  test("no recorded order: (added, id) fallback; no ship: empty, today's date", async () => {
    const { env, dir } = await makeEnv();
    const legacy = shipped();
    delete legacy.last_shipped_ids;
    await writeState(dir, legacy);
    const body = await json<Manifest>(
      await handle(env, "GET", "manifest", req("GET", "manifest?scope=live")),
    );
    expect(body.scenes.map((s) => s.id)).toEqual(["s1", "s2", "s3"]);

    legacy.last_shipped = null;
    await writeState(dir, legacy);
    const none = await json<Manifest>(
      await handle(env, "GET", "manifest", req("GET", "manifest?scope=live")),
    );
    expect(none.scenes).toEqual([]);
    expect(none.date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });
});
