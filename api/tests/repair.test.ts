import { describe, expect, test } from "bun:test";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { handle } from "../src/routes.ts";
import type { ApiScene, Manifest, SceneList } from "../src/scenes.ts";
import type { FeedbackFile, RepairEntry } from "../src/types.ts";
import { makeEnv } from "./helpers.ts";

/**
 * "Needs work" repair requests (ticket #2590): the moderation view's third
 * button stores the ticked reasons and takes the scene out of the queue
 * (moderation "repair"). The heartbeat's repair pass resolves the request;
 * the API's job is the store, the queue exclusion and the resolved marker.
 */

const BASE = "http://test.local/anomalyguessr/api/scenes/beta-street";

function moderate(body: unknown): Request {
  return new Request(`${BASE}/moderate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function get(rest: string): Request {
  return new Request(`http://test.local/anomalyguessr/api/${rest}`, {
    method: "GET",
  });
}

async function feedback(dir: string): Promise<FeedbackFile> {
  try {
    return JSON.parse(
      await readFile(path.join(dir, "feedback.json"), "utf8"),
    ) as FeedbackFile;
  } catch {
    return {
      version: 1,
      rejected: {},
      accepted: {},
      comments: {},
      reasons: {},
    };
  }
}

/** Write a resolved repair entry into the data dir's feedback.json. */
async function resolveRepair(
  dir: string,
  id: string,
  entry: RepairEntry,
): Promise<void> {
  const fb = await feedback(dir);
  fb.repairs = { ...(fb.repairs ?? {}), [id]: entry };
  await writeFile(
    path.join(dir, "feedback.json"),
    `${JSON.stringify(fb, null, 2)}\n`,
  );
}

describe("needs-work repair requests (#2590)", () => {
  test("stores the ticked reasons and takes the scene out of the queue", async () => {
    const { env, dir } = await makeEnv();
    const res = await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      moderate({
        action: "needs_work",
        reasons: ["click area doesn't cover anomaly"],
        feedback: "circle sits left of the watch",
      }),
    );
    expect(res.status).toBe(200);
    const body = (await res.json()) as { moderation: string };
    expect(body.moderation).toBe("repair");

    const fb = await feedback(dir);
    expect(fb.repairs?.["beta-street"]?.status).toBe("requested");
    expect(fb.repairs?.["beta-street"]?.reasons).toEqual([
      "click area doesn't cover anomaly",
    ]);
    expect(typeof fb.repairs?.["beta-street"]?.requestedAt).toBe("string");
    // The free-text note rides along as a comment, the verdicts stay empty.
    expect(fb.comments["beta-street"]?.map((c) => c.text)).toEqual([
      "circle sits left of the watch",
    ]);
    expect(fb.accepted["beta-street"]).toBeUndefined();
    expect(fb.rejected["beta-street"]).toBeUndefined();

    const list = (await (
      await handle(env, "GET", "scenes", get("scenes"))
    ).json()) as SceneList;
    const beta = list.scenes.find((s) => s.id === "beta-street");
    expect(beta?.moderation).toBe("repair");
    expect(beta?.repair?.status).toBe("requested");
    expect(list.summary.repair).toBe(1);
  });

  test("requires at least one canonical reason", async () => {
    const { env, dir } = await makeEnv();
    const res = await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      moderate({ action: "needs_work", reasons: [] }),
    );
    expect(res.status).toBe(400);
    const fb = await feedback(dir);
    expect(fb.repairs?.["beta-street"]).toBeUndefined();
  });

  test("a pending request leaves the unmoderated queue", async () => {
    const { env } = await makeEnv();
    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      moderate({ action: "needs_work", reasons: ["text is inaccurate"] }),
    );
    const unmod = (await (
      await handle(env, "GET", "manifest", get("manifest?scope=unmoderated"))
    ).json()) as Manifest;
    expect(unmod.scenes.map((s) => s.id)).toEqual(["alpha-market"]);
  });

  test("a repaired scene returns to the queue with its marker", async () => {
    const { env, dir } = await makeEnv();
    await resolveRepair(dir, "beta-street", {
      status: "repaired",
      reasons: ["click area doesn't cover anomaly"],
      requestedAt: "2026-10-13T06:00:00+02:00",
      resolvedAt: "2026-10-13T06:01:00+02:00",
      kind: "click",
      detail: "re-localized on the vision box",
      answerBefore: { x: 0.4, y: 0.5, r: 0.05 },
      answerAfter: { x: 0.55, y: 0.52, r: 0.06 },
    });
    const unmod = (await (
      await handle(env, "GET", "manifest", get("manifest?scope=unmoderated"))
    ).json()) as Manifest;
    expect(unmod.scenes.map((s) => s.id)).toEqual([
      "beta-street",
      "alpha-market",
    ]);
    const beta = unmod.scenes.find((s) => s.id === "beta-street");
    expect(beta?.repair?.status).toBe("repaired");
    expect(beta?.repair?.detail).toBe("re-localized on the vision box");

    const list = (await (
      await handle(env, "GET", "scenes", get("scenes"))
    ).json()) as SceneList;
    const apiBeta = list.scenes.find((s) => s.id === "beta-street") as ApiScene;
    expect(apiBeta.moderation).toBe("unmoderated");
    expect(apiBeta.repair?.answerAfter).toEqual({ x: 0.55, y: 0.52, r: 0.06 });
  });

  test("an unrepairable scene stays rejected and keeps its record", async () => {
    const { env, dir } = await makeEnv();
    const fb0 = await feedback(dir);
    fb0.rejected["beta-street"] = "2026-10-13T06:00:00+02:00";
    await writeFile(
      path.join(dir, "feedback.json"),
      `${JSON.stringify(fb0, null, 2)}\n`,
    );
    await resolveRepair(dir, "beta-street", {
      status: "unrepairable",
      reasons: ["too easy"],
      requestedAt: "2026-10-13T05:00:00+02:00",
      resolvedAt: "2026-10-13T06:01:00+02:00",
      kind: "none",
      detail: "too easy is not fixable by a repair",
    });
    const list = (await (
      await handle(env, "GET", "scenes", get("scenes"))
    ).json()) as SceneList;
    const beta = list.scenes.find((s) => s.id === "beta-street");
    expect(beta?.moderation).toBe("rejected");
    expect(beta?.repair?.status).toBe("unrepairable");
  });

  test("a human accept supersedes an open request", async () => {
    const { env, dir } = await makeEnv();
    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      moderate({ action: "needs_work", reasons: ["too easy"] }),
    );
    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      moderate({ action: "accept" }),
    );
    const fb = await feedback(dir);
    expect(fb.repairs?.["beta-street"]).toBeUndefined();
    expect(fb.accepted["beta-street"]).toBeDefined();
  });
});
