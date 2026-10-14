/**
 * The "remoderate" scope and the traceable re-decision (ticket #2606): the
 * dev moderation view's second mode walks the scenes that already carry a
 * verdict, oldest verdict first, and a new verdict overwrites the old one
 * while keeping it in `feedback.json.previous`.
 */

import { describe, expect, test } from "bun:test";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { handle } from "../src/routes.ts";
import type { Manifest } from "../src/scenes.ts";
import type { FeedbackFile, VerdictEntry } from "../src/types.ts";
import { makeEnv } from "./helpers.ts";

function req(method: string, url: string, body?: unknown): Request {
  const init: RequestInit = { method };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  return new Request(`http://test.local/anomalyguessr/api/${url}`, init);
}

async function json<T>(res: Response): Promise<T> {
  return (await res.json()) as T;
}

/** Write a feedback.json with the given verdict timestamps. */
async function writeFeedback(
  dir: string,
  accepted: Record<string, string>,
  rejected: Record<string, string>,
): Promise<void> {
  const fb = {
    version: 1,
    accepted,
    rejected,
    comments: {},
    reasons: {},
    funny: {},
    rating: {},
  };
  await writeFile(path.join(dir, "feedback.json"), `${JSON.stringify(fb)}\n`);
}

async function readFeedback(dir: string): Promise<FeedbackFile> {
  return JSON.parse(
    await readFile(path.join(dir, "feedback.json"), "utf8"),
  ) as FeedbackFile;
}

async function remoderateIds(env: unknown): Promise<string[]> {
  const res = await handle(
    env as Parameters<typeof handle>[0],
    "GET",
    "manifest",
    req("GET", "manifest?scope=remoderate"),
  );
  return (await json<Manifest>(res)).scenes.map((s) => s.id);
}

describe("remoderate scope (#2606)", () => {
  test("lists the decided scenes, oldest verdict first", async () => {
    const { env, dir } = await makeEnv();
    await writeFeedback(
      dir,
      { "alpha-market": "2026-09-20T00:00:00Z" },
      { "beta-street": "2026-09-08T00:00:00Z" },
    );
    expect(await remoderateIds(env)).toEqual(["beta-street", "alpha-market"]);
  });

  test("an unmoderated scene never appears", async () => {
    const { env } = await makeEnv();
    // no feedback at all -> nothing decided
    expect(await remoderateIds(env)).toEqual([]);
    // beta gets a verdict; alpha stays unmoderated and out of the mode
    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      req("POST", "x", { action: "accept" }),
    );
    expect(await remoderateIds(env)).toEqual(["beta-street"]);
  });

  test("a changed verdict overwrites the old one and keeps it traceable", async () => {
    const { env, dir } = await makeEnv();
    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      req("POST", "x", { action: "accept" }),
    );
    let fb = await readFeedback(dir);
    expect(fb.previous?.["beta-street"]).toBeUndefined(); // first judgement
    const firstAccept = fb.accepted["beta-street"];

    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      req("POST", "x", { action: "reject", reasons: ["too easy"] }),
    );
    fb = await readFeedback(dir);
    expect(fb.rejected["beta-street"]).toBeDefined();
    expect(fb.accepted["beta-street"]).toBeUndefined();
    const previous: VerdictEntry | undefined = fb.previous?.["beta-street"];
    expect(previous?.verdict).toBe("accepted");
    expect(previous?.at).toBe(firstAccept);

    // the other direction records the rejection it replaced
    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      req("POST", "x", { action: "accept" }),
    );
    fb = await readFeedback(dir);
    expect(fb.previous?.["beta-street"]?.verdict).toBe("rejected");
  });

  test("re-accepting an already-accepted scene changes nothing", async () => {
    const { env, dir } = await makeEnv();
    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      req("POST", "x", { action: "accept" }),
    );
    const at = (await readFeedback(dir)).accepted["beta-street"];
    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      req("POST", "x", { action: "accept" }),
    );
    const fb = await readFeedback(dir);
    expect(fb.accepted["beta-street"]).toBe(at);
    expect(fb.previous?.["beta-street"]).toBeUndefined();
  });

  test("a scene re-accepted here returns to the shippable pool", async () => {
    const { env, dir } = await makeEnv();
    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      req("POST", "x", { action: "reject", reasons: ["too easy"] }),
    );
    // beta is rejected: out of the accepted scope
    let accepted = await json<Manifest>(
      await handle(
        env,
        "GET",
        "manifest",
        req("GET", "manifest?scope=accepted"),
      ),
    );
    expect(accepted.scenes.map((s) => s.id)).toEqual([]);
    // re-accept it in the remoderate mode
    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      req("POST", "x", { action: "accept" }),
    );
    accepted = await json<Manifest>(
      await handle(
        env,
        "GET",
        "manifest",
        req("GET", "manifest?scope=accepted"),
      ),
    );
    expect(accepted.scenes.map((s) => s.id)).toEqual(["beta-street"]);
    // the rejection is gone, the change is recorded
    const fb = await readFeedback(dir);
    expect(fb.rejected["beta-street"]).toBeUndefined();
    expect(fb.previous?.["beta-street"]?.verdict).toBe("rejected");
    // and the scene is now part of the remoderate queue with the fresh verdict
    expect(await remoderateIds(env)).toEqual(["beta-street"]);
  });
});
