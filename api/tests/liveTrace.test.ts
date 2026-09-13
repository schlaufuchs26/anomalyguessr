import { describe, expect, test } from "bun:test";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { type GenerateStatus, writeStatus } from "../src/generate.ts";
import { type Env, handle } from "../src/routes.ts";
import { makeEnv } from "./helpers.ts";

/**
 * The live generation trace (ticket #1446).
 *
 * The moderation queue's Generate button polls GET /generate while a run is
 * in flight. That payload now also carries the steps of the trace the
 * pipeline is writing (traces/pending/<slug>.json), metadata only; the text
 * of an opened step comes from GET /traces/{slug}. These tests drive the
 * poll payload from a fixture pending dir.
 */

/** The pending trace file is keyed by a slug; this source id already is one. */
const SOURCE = "commons-market-abc123";

/** One pipeline step as the trace stores it (text fields are large on purpose). */
function step(stage: string, extra: Record<string, unknown> = {}) {
  return {
    stage,
    model: "deepseek/deepseek-v4.1-flash",
    prompt: "x".repeat(2000),
    answer: "y".repeat(2000),
    reasoning: "because the market predates PET",
    duration_s: 3.2,
    at: "2026-09-13T15:40:00+02:00",
    usage: {
      prompt_tokens: 900,
      completion_tokens: 120,
      reasoning_tokens: 0,
      cost: 0.0003,
    },
    ...extra,
  };
}

/** Write a pending trace file the way ag_generate.flush_trace does. */
async function writePending(
  dir: string,
  ref: string,
  calls: unknown[],
  extra: Record<string, unknown> = {},
): Promise<void> {
  const file = path.join(dir, "traces", "pending", `${ref}.json`);
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, JSON.stringify({ source: SOURCE, calls, ...extra }));
}

async function status(env: Env): Promise<GenerateStatus> {
  const res = await handle(env, "GET", "generate", new Request("http://t/x"));
  expect(res.status).toBe(200);
  return (await res.json()) as GenerateStatus;
}

describe("GET /generate live trace (#1446)", () => {
  test("an idle generator exposes no live trace", async () => {
    const { env } = await makeEnv();
    expect((await status(env)).liveTrace).toBeUndefined();
  });

  test("steps appear between two polls, metadata only", async () => {
    const { env, dir } = await makeEnv();
    // The run has started but has not flushed its first step yet: an empty
    // list with the poll's ref, not an error.
    await writeStatus(dir, { state: "running", count: 2, scene: SOURCE });
    const start = await status(env);
    expect(start.liveTrace).toEqual({ ref: SOURCE, steps: [] });

    await writePending(dir, SOURCE, [step("proposal", { attempt: 1 })]);
    const first = await status(env);
    expect(first.liveTrace?.steps.length).toBe(1);
    const firstStep = first.liveTrace?.steps[0];
    expect(firstStep?.stage).toBe("proposal");
    expect(firstStep?.model).toBe("deepseek/deepseek-v4.1-flash");
    expect(firstStep?.duration_s).toBe(3.2);
    // The large text stays behind the trace endpoint.
    expect(firstStep?.prompt).toBeUndefined();
    expect(firstStep?.answer).toBeUndefined();
    expect(firstStep?.reasoning).toBeUndefined();
    expect(JSON.stringify(first.liveTrace)).not.toContain("xxx");

    // The pipeline appends the next step; the next poll carries it.
    await writePending(dir, SOURCE, [
      step("proposal", { attempt: 1 }),
      step("edit r0", { round: 0, draw: 1, seed: 42 }),
    ]);
    const second = await status(env);
    expect(second.liveTrace?.steps.map((s) => s.stage)).toEqual([
      "proposal",
      "edit r0",
    ]);
    expect(second.liveTrace?.steps[1]?.seed).toBe(42);
  });

  test("checker and click-target fields ride along for the live labels", async () => {
    const { env, dir } = await makeEnv();
    await writeStatus(dir, { state: "running", scene: SOURCE });
    await writePending(dir, SOURCE, [
      step("check r1", { attempt: 1, score: 5, failed: [3, 7] }),
      step("click-target 1", {
        verdict_reason: "the box sticks out of the circle",
        box: { x1: 0.1, y1: 0.1, x2: 0.4, y2: 0.4 },
        covers: false,
        corrected: { x: 0.3, y: 0.3, r: 0.2 },
      }),
    ]);
    const out = await status(env);
    expect(out.liveTrace?.steps[0]?.score).toBe(5);
    expect(out.liveTrace?.steps[0]?.failed).toEqual([3, 7]);
    expect(out.liveTrace?.steps[1]?.verdict_reason).toBe(
      "the box sticks out of the circle",
    );
    expect(out.liveTrace?.steps[1]?.covers).toBe(false);
  });

  test("an absent or unreadable pending file is an empty list, not an error", async () => {
    const { env, dir } = await makeEnv();
    await writeStatus(dir, { state: "running", scene: SOURCE });
    expect((await status(env)).liveTrace).toEqual({ ref: SOURCE, steps: [] });

    await writePending(dir, SOURCE, []);
    await writeFile(
      path.join(dir, "traces", "pending", `${SOURCE}.json`),
      "{ half-written",
    );
    expect((await status(env)).liveTrace?.steps).toEqual([]);
  });

  test("only the active run's pending trace is shown", async () => {
    const { env, dir } = await makeEnv();
    // A crashed earlier run left its own pending file behind; the status
    // names the scene being worked on now, so that one wins.
    await writePending(dir, "commons-stale-000000", [step("proposal")]);
    await writePending(dir, SOURCE, [step("check r0", { score: 7 })]);
    await writeStatus(dir, { state: "running", scene: SOURCE });

    const out = await status(env);
    expect(out.liveTrace?.ref).toBe(SOURCE);
    expect(out.liveTrace?.steps.map((s) => s.stage)).toEqual(["check r0"]);
  });

  test("a finished run goes quiet and names the landed scene", async () => {
    const { env, dir } = await makeEnv();
    await writePending(dir, SOURCE, [step("proposal")]);
    await writeStatus(dir, {
      state: "done",
      sceneId: "ag-42-plastic-bottle",
      added: 1,
    });
    const out = await status(env);
    expect(out.liveTrace).toBeUndefined();
    expect(out.sceneId).toBe("ag-42-plastic-bottle");
  });

  test("the trace's own error note rides with the steps", async () => {
    const { env, dir } = await makeEnv();
    await writeStatus(dir, { state: "running", scene: SOURCE });
    await writePending(dir, SOURCE, [step("edit r0", { error: "refused" })], {
      error: "image edit refused (safety)",
    });
    const out = await status(env);
    expect(out.liveTrace?.error).toBe("image edit refused (safety)");
  });
});

describe("GET /traces/{ref} for the in-flight trace (#1446)", () => {
  test("serves the pending file by its slug while the run writes it", async () => {
    const { env, dir } = await makeEnv();
    await writePending(dir, SOURCE, [step("proposal")]);
    const res = await handle(
      env,
      "GET",
      `traces/${SOURCE}`,
      new Request("http://t/x"),
    );
    expect(res.status).toBe(200);
    const body = (await res.json()) as { calls: { prompt: string }[] };
    // the full text is here, unlike in the poll payload
    expect(body.calls.length).toBe(1);
    expect(body.calls[0]?.prompt.length).toBe(2000);
  });

  test("404s once the in-flight trace is gone, and for path tricks", async () => {
    const { env } = await makeEnv();
    for (const rest of [
      `traces/${SOURCE}`,
      "traces/..%2f..%2fstate",
      "traces/Alpha_Market",
    ]) {
      const res = await handle(env, "GET", rest, new Request("http://t/x"));
      expect(res.status).toBe(404);
    }
  });
});
