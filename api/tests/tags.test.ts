import { describe, expect, test } from "bun:test";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { handle } from "../src/routes.ts";
import type { SceneList } from "../src/scenes.ts";
import type { FeedbackFile } from "../src/types.ts";
import { makeEnv } from "./helpers.ts";

// The "funny" tag (#1502) and the 0-10 quality rating (#2605, replacing the
// old "great" tick): labels next to the accept/reject verdict, each
// independent of the verdict and of the other. Their own file keeps the route
// tests under the 500-line gate.

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

describe("moderation tag + rating (#1502/#2605)", () => {
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

  test("rating: 0-10 sets, persists, shows on the scene, clears with null", async () => {
    const { env, dir } = await makeEnv();
    expect(
      (
        await handle(
          env,
          "POST",
          "scenes/nope/rating",
          req("POST", "x", { rating: 8 }),
        )
      ).status,
    ).toBe(404);

    const on = await json<{
      id: string;
      rating: number | null;
      ratingAt: string | null;
    }>(
      await handle(
        env,
        "POST",
        "scenes/beta-street/rating",
        req("POST", "x", { rating: 8 }),
      ),
    );
    expect(on.rating).toBe(8);
    expect(on.ratingAt).toBeTruthy();

    const list = await json<SceneList>(
      await handle(env, "GET", "scenes", req("GET", "scenes")),
    );
    const beta = list.scenes.find((s) => s.id === "beta-street");
    expect(beta?.rating).toBe(8);
    expect(beta?.ratingAt).toBeTruthy();
    // A never-rated scene reads null, not 0.
    const alpha = list.scenes.find((s) => s.id === "alpha-market");
    expect(alpha?.rating).toBeNull();

    const stored = await feedback(dir);
    expect(stored.rating?.["beta-street"]?.value).toBe(8);
    expect(stored.rating?.["beta-street"]?.at).toBeTruthy();

    // 0 is a real rating, distinct from unrated.
    const zero = await json<{ rating: number | null }>(
      await handle(
        env,
        "POST",
        "scenes/beta-street/rating",
        req("POST", "x", { rating: 0 }),
      ),
    );
    expect(zero.rating).toBe(0);
    expect((await feedback(dir)).rating?.["beta-street"]?.value).toBe(0);

    // null clears it again.
    const cleared = await json<{ rating: number | null }>(
      await handle(
        env,
        "POST",
        "scenes/beta-street/rating",
        req("POST", "x", { rating: null }),
      ),
    );
    expect(cleared.rating).toBeNull();
    expect((await feedback(dir)).rating).toEqual({});
  });

  test("rating rejects values outside the 0-10 scale", async () => {
    const { env } = await makeEnv();
    for (const bad of [11, -1, 3.5, "8", true]) {
      const res = await handle(
        env,
        "POST",
        "scenes/beta-street/rating",
        req("POST", "x", { rating: bad }),
      );
      expect(res.status).toBe(400);
    }
  });

  test("the tag and the rating stay independent of each other and the verdict", async () => {
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
      "scenes/beta-street/rating",
      req("POST", "x", { rating: 9 }),
    );

    const list = await json<SceneList>(
      await handle(env, "GET", "scenes", req("GET", "scenes")),
    );
    const alpha = list.scenes.find((s) => s.id === "alpha-market");
    const beta = list.scenes.find((s) => s.id === "beta-street");
    // Each scene carries only the mark it got.
    expect(alpha?.funny).toBe(true);
    expect(alpha?.rating).toBeNull();
    expect(beta?.funny).toBe(false);
    expect(beta?.rating).toBe(9);

    // A verdict leaves the rating alone.
    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      req("POST", "x", { action: "accept" }),
    );
    expect((await feedback(dir)).rating?.["beta-street"]?.value).toBe(9);
  });

  test("a legacy great tick reads as a rating of 10 and folds on the next write", async () => {
    const { env, dir } = await makeEnv();
    const legacy: FeedbackFile = {
      version: 1,
      rejected: {},
      accepted: {},
      comments: {},
      reasons: {},
      funny: {},
      great: { "beta-street": "2026-09-01T00:00:00Z" },
    };
    await writeFile(
      path.join(dir, "feedback.json"),
      `${JSON.stringify(legacy, null, 2)}\n`,
    );

    const list = await json<SceneList>(
      await handle(env, "GET", "scenes", req("GET", "scenes")),
    );
    expect(list.scenes.find((s) => s.id === "beta-street")?.rating).toBe(10);

    // Any write drops the old key and keeps the folded rating.
    await handle(
      env,
      "POST",
      "scenes/alpha-market/funny",
      req("POST", "x", { tag: true }),
    );
    const after = await feedback(dir);
    expect(after.great).toBeUndefined();
    expect(after.rating?.["beta-street"]?.value).toBe(10);
  });
});
