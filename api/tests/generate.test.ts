import { describe, expect, test } from "bun:test";
import path from "node:path";
import {
  type GenerateStatus,
  lockHeld,
  spawnGenerator,
  writeStatus,
} from "../src/generate.ts";
import type { Env } from "../src/routes.ts";
import { handle } from "../src/routes.ts";
import { makeEnv } from "./helpers.ts";

function req(method: string, body?: unknown): Request {
  const init: RequestInit = { method };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  return new Request("http://test.local/anomalyguessr/api/generate", init);
}

async function status(env: Env): Promise<GenerateStatus> {
  const res = await handle(env, "GET", "generate", req("GET"));
  return (await res.json()) as GenerateStatus;
}

/** Polls the status until the run leaves "running" or the deadline passes. */
async function waitRun(env: Env): Promise<GenerateStatus> {
  const deadline = Date.now() + 2000;
  for (;;) {
    const out = await status(env);
    if (out.state !== "running") return out;
    if (Date.now() > deadline)
      throw new Error(`run did not finish: ${JSON.stringify(out)}`);
    await Bun.sleep(5);
  }
}

describe("GET /generate", () => {
  test("idle with an empty buffer; buffer counts unshown accepted scenes", async () => {
    const { env } = await makeEnv();
    expect(await status(env)).toMatchObject({
      state: "idle",
      running: false,
      buffer: 0,
    });

    // accept the unshown beta (the buffer) and the shown alpha (not)
    for (const id of ["beta-street", "alpha-market"]) {
      await handle(
        env,
        "POST",
        `scenes/${id}/moderate`,
        req("POST", { action: "accept" }),
      );
    }
    const out = await status(env);
    expect(out.buffer).toBe(1);
    expect(out.state).toBe("idle");
  });

  test("a stale running status file is reported as an error, keeping progress", async () => {
    const { env, dir } = await makeEnv();
    await writeStatus(dir, {
      state: "running",
      count: 5,
      planned: 5,
      added: 3,
    });
    const out = await status(env);
    expect(out.state).toBe("error");
    expect(out.running).toBe(false);
    expect(out.error).toBeTruthy();
    expect(out.added).toBe(3);
  });

  test("an error status file surfaces its message", async () => {
    const { env, dir } = await makeEnv();
    await writeStatus(dir, {
      state: "error",
      count: 5,
      error: "no unused sources",
      finishedAt: "2026-09-11T18:00:00+02:00",
    });
    const out = await status(env);
    expect(out.state).toBe("error");
    expect(out.error).toBe("no unused sources");
  });
});

describe("POST /generate", () => {
  test("starts a batch, refuses a second start while it runs, then ends done", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let started: () => void = () => {};
    const startedGate = new Promise<void>((resolve) => {
      started = resolve;
    });
    let gotDir = "";
    let gotCount = 0;
    const { env, dir } = await makeEnv(async (dataDir, count) => {
      gotDir = dataDir;
      gotCount = count;
      started();
      await gate;
      await writeStatus(dataDir, {
        state: "done",
        count,
        planned: count,
        added: 2,
        finishedAt: "2026-09-11T18:00:00+02:00",
      });
    });

    const res = await handle(env, "POST", "generate", req("POST"));
    expect(res.status).toBe(200);
    const start = (await res.json()) as GenerateStatus;
    expect(start.state).toBe("running");
    expect(start.running).toBe(true);
    expect(start.count).toBe(5); // one daily set by default

    await startedGate;
    expect(gotDir).toBe(dir);
    expect(gotCount).toBe(5);

    // a second start while the first is in flight is refused
    expect((await handle(env, "POST", "generate", req("POST"))).status).toBe(
      409,
    );

    release();
    const final = await waitRun(env);
    expect(final.state).toBe("done");
    expect(final.added).toBe(2);
    expect(final.running).toBe(false);
  });

  test("explicit count; 0 falls back to the default; out of range is 400", async () => {
    const seen: number[] = [];
    const { env } = await makeEnv(async (dataDir, count) => {
      seen.push(count);
      await writeStatus(dataDir, { state: "done", count });
    });

    expect(
      (await handle(env, "POST", "generate", req("POST", { count: 3 }))).status,
    ).toBe(200);
    await waitRun(env);
    expect(seen).toEqual([3]);

    // count 0 is "unset" (Go's int zero value), not an error
    expect(
      (await handle(env, "POST", "generate", req("POST", { count: 0 }))).status,
    ).toBe(200);
    await waitRun(env);
    expect(seen).toEqual([3, 5]);

    expect(
      (await handle(env, "POST", "generate", req("POST", { count: 99 })))
        .status,
    ).toBe(400);
    expect(
      (await handle(env, "POST", "generate", req("POST", { count: -1 })))
        .status,
    ).toBe(400);
    expect(
      (await handle(env, "POST", "generate", req("POST", { count: 1.5 })))
        .status,
    ).toBe(400);
    expect(seen).toEqual([3, 5]); // no run started by the rejected counts
  });

  test("refused while the cron holds the run lock, and the status says running", async () => {
    let called = false;
    const { env, dir } = await makeEnv(async () => {
      called = true;
    });
    // Hold the same flock pipeline/ag_generate.py takes, as a cron run would.
    // -w (not -n) so a probe racing the startup cannot make the holder give
    // up; -F makes flock exec the sleep in place, so killing it releases the
    // lock instead of leaving a child with the inherited fd behind.
    const lockPath = path.join(dir, "generate.lock");
    const holder = Bun.spawn([
      "flock",
      "-F",
      "-w",
      "5",
      lockPath,
      "sleep",
      "30",
    ]);
    const deadline = Date.now() + 2000;
    while (!lockHeld(dir)) {
      if (Date.now() > deadline) {
        throw new Error(
          `lock holder never took the lock (rc ${holder.exitCode})`,
        );
      }
      await Bun.sleep(5);
    }

    expect((await handle(env, "POST", "generate", req("POST"))).status).toBe(
      409,
    );
    expect(called).toBe(false);

    // the status reflects the cron's run even though this API did not start it
    const out = await status(env);
    expect(out.state).toBe("running");
    expect(out.running).toBe(true);

    holder.kill();
    await holder.exited;
    while (lockHeld(dir)) {
      if (Date.now() > deadline)
        throw new Error("lock still held after the kill");
      await Bun.sleep(5);
    }
    const idle = await status(env);
    expect(idle.running).toBe(false);
    expect(idle.state).toBe("idle");
  });

  test("a runner failure surfaces as state=error with its message", async () => {
    const { env } = await makeEnv(async () => {
      throw new Error("python3: executable file not found");
    });
    const res = await handle(env, "POST", "generate", req("POST"));
    expect(res.status).toBe(200);
    const final = await waitRun(env);
    expect(final.state).toBe("error");
    expect(final.error).toContain("executable file not found");
  });
});

describe("spawnGenerator", () => {
  test("a missing generator script is a clear error, not a silent no-op", async () => {
    const { dir } = await makeEnv();
    await expect(spawnGenerator(dir, dir, dir, dir, 5)).rejects.toThrow(
      "generator script not found",
    );
  });
});
