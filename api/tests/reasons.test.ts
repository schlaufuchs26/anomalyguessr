import { describe, expect, test } from "bun:test";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { REJECT_REASONS } from "../../rejectReasons.ts";
import { handle } from "../src/routes.ts";
import { makeEnv } from "./helpers.ts";

/**
 * Canonical rejection reasons (ticket #1627): the moderation view's five
 * one-click buttons post `{action: "reject", reason, feedback}`, and the API
 * stores the reason structured in feedback.json. An unknown string stays a
 * plain comment so an older client cannot break. Split from routes.test.ts
 * to stay under the 500-line gate.
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
  reasons: Record<string, { reason: string; at: string }>;
  comments: Record<string, { text: string }[]>;
}

async function stored(dir: string): Promise<StoredFeedback> {
  return JSON.parse(
    await readFile(path.join(dir, "feedback.json"), "utf8"),
  ) as StoredFeedback;
}

describe("rejection reasons (#1627)", () => {
  test("all five canonical reasons are accepted and stored structured", async () => {
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
    expect(fb.reasons["beta-street"]?.reason).toBe(
      REJECT_REASONS[REJECT_REASONS.length - 1],
    );
    expect(typeof fb.reasons["beta-street"]?.at).toBe("string");
  });

  test("a reason with a typed note stores both, without duplicating prose", async () => {
    const { env, dir } = await makeEnv();
    const res = await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      moderate({
        action: "reject",
        reason: "scaling of anomaly is wrong",
        feedback: "the car is huge",
      }),
    );
    expect(res.status).toBe(200);
    const fb = await stored(dir);
    expect(fb.reasons["beta-street"]?.reason).toBe(
      "scaling of anomaly is wrong",
    );
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
      moderate({ action: "reject", reason: "too easy" }),
    );
    await handle(env, "POST", "scenes/beta-street/restore", restore());
    const fb = await stored(dir);
    expect(fb.reasons["beta-street"]).toBeUndefined();
  });
});
