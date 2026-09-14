import { describe, expect, test } from "bun:test";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { handle } from "../src/routes.ts";
import type { SceneList } from "../src/scenes.ts";
import { makeEnv } from "./helpers.ts";

// The optional "lustig" moderation tag (ticket #1502): a separate, one-way
// label next to the accept/reject verdict. Its own file keeps the route tests
// under the 500-line gate.

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

describe("funny tag (#1502)", () => {
  test("tags, persists to feedback.json, shows on the scene, toggles off", async () => {
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

    const fb = JSON.parse(
      await readFile(path.join(dir, "feedback.json"), "utf8"),
    ) as { funny: Record<string, string> };
    expect(Object.keys(fb.funny)).toEqual(["beta-street"]);

    // toggle without a body flips it off again
    const off = await json<{ funny: boolean }>(
      await handle(env, "POST", "scenes/beta-street/funny", req("POST", "x")),
    );
    expect(off.funny).toBe(false);
    const fb2 = JSON.parse(
      await readFile(path.join(dir, "feedback.json"), "utf8"),
    ) as { funny: Record<string, string> };
    expect(fb2.funny).toEqual({});
  });
});
