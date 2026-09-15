import { describe, expect, test } from "bun:test";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { handle } from "../src/routes.ts";
import type { ApiScene, SceneList } from "../src/scenes.ts";
import { makeEnv, scene, writeState } from "./helpers.ts";

// The gallery's "needs review" badge (#1449): the stored flag marks a scene
// whose checker chain did not pass cleanly. Only an undecided scene has
// something left to decide, so the served payload drops the flag once an
// accept/reject verdict exists (ticket #1532). The stored entry and the
// checker verdict keep it.

function req(method: string, rest: string, body?: unknown): Request {
  const init: RequestInit = { method };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  return new Request(`http://test.local/anomalyguessr/api/${rest}`, init);
}

describe("needsReview payload rule (#1532)", () => {
  test("served only while a decision is open", async () => {
    const { env, dir } = await makeEnv();
    await writeState(dir, {
      version: 1,
      last_shipped: null,
      scenes: {
        "alpha-market": {
          ...scene("alpha-market", "2026-09-01", "2026-09-07"),
          needs_review: true,
        },
        "beta-street": {
          ...scene("beta-street", "2026-09-08", null),
          needs_review: true,
        },
      },
    });
    const flags = async (): Promise<(boolean | undefined)[]> => {
      const res = await handle(env, "GET", "scenes", req("GET", "scenes"));
      const body = (await res.json()) as SceneList;
      return body.scenes.map((s) => s.needsReview);
    };

    // two flagged scenes, neither decided: the badge shows on both
    expect(await flags()).toEqual([true, true]);

    // accepting one clears its badge and leaves the other scene's alone
    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      req("POST", "x", { action: "accept" }),
    );
    expect(await flags()).toEqual([undefined, true]);
    const one = (await (
      await handle(env, "GET", "scenes/beta-street", req("GET", "x"))
    ).json()) as ApiScene;
    expect(one.needsReview).toBeUndefined();

    // rejecting the other clears it too; the stored flag itself survives
    await handle(
      env,
      "POST",
      "scenes/alpha-market/moderate",
      req("POST", "x", { action: "reject" }),
    );
    expect(await flags()).toEqual([undefined, undefined]);
    const st = JSON.parse(
      await readFile(path.join(dir, "state.json"), "utf8"),
    ) as { scenes: Record<string, { needs_review?: boolean }> };
    expect(st.scenes["alpha-market"]?.needs_review).toBe(true);
  });
});
