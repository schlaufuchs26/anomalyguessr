import { describe, expect, test } from "bun:test";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { REJECT_REASONS } from "../../rejectReasons.ts";
import { handle } from "../src/routes.ts";
import { makeEnv } from "./helpers.ts";

/**
 * Canonical rejection reasons (ticket #1627): the moderation view's one-click
 * buttons post `{action: "reject", reason, feedback}`, and the API stores the
 * reason structured in feedback.json. Since #2585 the view posts several
 * reasons at once (`reasons: [...]`) and they all survive; the legacy single
 * `reason` still works. An unknown string stays a plain comment so an older
 * client cannot break. Split from routes.test.ts to stay under the 500-line
 * gate.
 */

const BASE = "http://test.local/anomalyguessr/api/scenes/beta-street";

function moderate(body: unknown): Request {
  return new Request(`${BASE}/moderate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function restore(): Request {
  return new Request(`${BASE}/restore`, { method: "POST" });
}

interface StoredFeedback {
  reasons: Record<string, { reason: string | string[]; at: string }>;
  comments: Record<string, { text: string }[]>;
}

async function stored(dir: string): Promise<StoredFeedback> {
  return JSON.parse(
    await readFile(path.join(dir, "feedback.json"), "utf8"),
  ) as StoredFeedback;
}

describe("rejection reasons (#1627)", () => {
  test("all eight canonical reasons are accepted and stored structured", async () => {
    const { env, dir } = await makeEnv();
    for (const reason of REJECT_REASONS) {
      const res = await handle(
        env,
        "POST",
        "scenes/beta-street/moderate",
        moderate({ action: "reject", reason }),
      );
      expect(res.status).toBe(200);
    }
    const fb = await stored(dir);
    expect(fb.reasons["beta-street"]?.reason).toEqual([
      REJECT_REASONS.at(-1) ?? "",
    ]);
    expect(typeof fb.reasons["beta-street"]?.at).toBe("string");
  });

  test("several reasons survive one decision (#2585)", async () => {
    const { env, dir } = await makeEnv();
    const res = await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      moderate({
        action: "reject",
        reasons: ["too easy", "text is inaccurate"],
      }),
    );
    expect(res.status).toBe(200);
    const fb = await stored(dir);
    expect(fb.reasons["beta-street"]?.reason).toEqual([
      "too easy",
      "text is inaccurate",
    ]);
  });

  test("a repeated reason is stored once (#2585)", async () => {
    const { env, dir } = await makeEnv();
    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      moderate({ action: "reject", reasons: ["too easy", "too easy"] }),
    );
    const fb = await stored(dir);
    expect(fb.reasons["beta-street"]?.reason).toEqual(["too easy"]);
  });

  test("the legacy single `reason` field still works (#2585)", async () => {
    const { env, dir } = await makeEnv();
    const res = await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      moderate({ action: "reject", reason: "too hard" }),
    );
    expect(res.status).toBe(200);
    const fb = await stored(dir);
    expect(fb.reasons["beta-street"]?.reason).toEqual(["too hard"]);
  });

  test("a reason with a typed note stores both, without duplicating prose", async () => {
    const { env, dir } = await makeEnv();
    const res = await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      moderate({
        action: "reject",
        reasons: ["scaling of anomaly is wrong"],
        feedback: "the car is huge",
      }),
    );
    expect(res.status).toBe(200);
    const fb = await stored(dir);
    expect(fb.reasons["beta-street"]?.reason).toEqual([
      "scaling of anomaly is wrong",
    ]);
    expect(fb.comments["beta-street"]?.map((c) => c.text)).toEqual([
      "the car is huge",
    ]);
  });

  test("an unknown reason string stays allowed as a comment", async () => {
    const { env, dir } = await makeEnv();
    const res = await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      moderate({ action: "reject", reason: "my own wording" }),
    );
    expect(res.status).toBe(200);
    const fb = await stored(dir);
    expect(fb.reasons["beta-street"]).toBeUndefined();
    expect(fb.comments["beta-street"]?.map((c) => c.text)).toEqual([
      "my own wording",
    ]);
  });

  test("restoring a scene drops its stored reason", async () => {
    const { env, dir } = await makeEnv();
    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      moderate({ action: "reject", reasons: ["too easy"] }),
    );
    await handle(env, "POST", "scenes/beta-street/restore", restore());
    const fb = await stored(dir);
    expect(fb.reasons["beta-street"]).toBeUndefined();
  });
});
