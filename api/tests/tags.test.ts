import { describe, expect, test } from "bun:test";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { handle } from "../src/routes.ts";
import type { SceneList } from "../src/scenes.ts";
import type { FeedbackFile } from "../src/types.ts";
import { makeEnv } from "./helpers.ts";

// The two optional moderation tags (tickets #1502 and #1541): labels next to
// the accept/reject verdict, each independent of the verdict and of the other
// tag. Their own file keeps the route tests under the 500-line gate.

function req(method: string, url: string, body?: unknown): Request {
  const init: RequestInit = { method };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const full = url.startsWith("http")
    ? url
    : `http://test.local/anomalyguessr/api/${url}`;
  return new Request(full, init);
}

async function json<T>(res: Response): Promise<T> {
  return (await res.json()) as T;
}

async function feedback(dir: string): Promise<FeedbackFile> {
  return JSON.parse(
    await readFile(path.join(dir, "feedback.json"), "utf8"),
  ) as FeedbackFile;
}

describe("moderation tags (#1502/#1541)", () => {
  test("funny: tags, persists to feedback.json, shows on the scene, toggles off", async () => {
    const { env, dir } = await makeEnv();
    // unknown scene -> 404
    expect(
      (
        await handle(
          env,
          "POST",
          "scenes/nope/funny",
          req("POST", "x", { tag: true }),
        )
      ).status,
    ).toBe(404);

    const on = await json<{
      id: string;
      funny: boolean;
      funnyAt: string | null;
    }>(
      await handle(
        env,
        "POST",
        "scenes/beta-street/funny",
        req("POST", "x", { tag: true }),
      ),
    );
    expect(on.funny).toBe(true);
    expect(on.funnyAt).toBeTruthy();

    const list = await json<SceneList>(
      await handle(env, "GET", "scenes", req("GET", "scenes")),
    );
    const beta = list.scenes.find((s) => s.id === "beta-street");
    expect(beta?.funny).toBe(true);
    expect(beta?.funnyAt).toBeTruthy();

    expect(Object.keys((await feedback(dir)).funny ?? {})).toEqual([
      "beta-street",
    ]);

    // toggle without a body flips it off again
    const off = await json<{ funny: boolean }>(
      await handle(env, "POST", "scenes/beta-street/funny", req("POST", "x")),
    );
    expect(off.funny).toBe(false);
    expect((await feedback(dir)).funny).toEqual({});
  });

  test("great: same shape, its own store field and payload keys", async () => {
    const { env, dir } = await makeEnv();
    expect(
      (
        await handle(
          env,
          "POST",
          "scenes/nope/great",
          req("POST", "x", { tag: true }),
        )
      ).status,
    ).toBe(404);

    const on = await json<{
      id: string;
      great: boolean;
      greatAt: string | null;
    }>(
      await handle(
        env,
        "POST",
        "scenes/beta-street/great",
        req("POST", "x", { tag: true }),
      ),
    );
    expect(on.great).toBe(true);
    expect(on.greatAt).toBeTruthy();

    const list = await json<SceneList>(
      await handle(env, "GET", "scenes", req("GET", "scenes")),
    );
    const beta = list.scenes.find((s) => s.id === "beta-street");
    expect(beta?.great).toBe(true);
    expect(beta?.greatAt).toBeTruthy();

    expect(Object.keys((await feedback(dir)).great ?? {})).toEqual([
      "beta-street",
    ]);

    const off = await json<{ great: boolean }>(
      await handle(env, "POST", "scenes/beta-street/great", req("POST", "x")),
    );
    expect(off.great).toBe(false);
    expect((await feedback(dir)).great).toEqual({});
  });

  test("the tags stay independent of each other and of other scenes", async () => {
    const { env, dir } = await makeEnv();
    await handle(
      env,
      "POST",
      "scenes/alpha-market/funny",
      req("POST", "x", { tag: true }),
    );
    await handle(
      env,
      "POST",
      "scenes/beta-street/great",
      req("POST", "x", { tag: true }),
    );

    const list = await json<SceneList>(
      await handle(env, "GET", "scenes", req("GET", "scenes")),
    );
    const alpha = list.scenes.find((s) => s.id === "alpha-market");
    const beta = list.scenes.find((s) => s.id === "beta-street");
    // Each scene carries only the tag it got.
    expect(alpha?.funny).toBe(true);
    expect(alpha?.great).toBe(false);
    expect(beta?.funny).toBe(false);
    expect(beta?.great).toBe(true);

    const fb = await feedback(dir);
    expect(fb.funny).toEqual({ "alpha-market": expect.any(String) });
    expect(fb.great).toEqual({ "beta-street": expect.any(String) });

    // Setting the second tag on one scene leaves its first tag untouched.
    await handle(
      env,
      "POST",
      "scenes/alpha-market/great",
      req("POST", "x", { tag: true }),
    );
    const after = await feedback(dir);
    expect(after.funny).toEqual({ "alpha-market": expect.any(String) });
    expect(Object.keys(after.great ?? {}).sort()).toEqual([
      "alpha-market",
      "beta-street",
    ]);
  });
});
