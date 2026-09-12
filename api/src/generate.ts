import { closeSync, existsSync, openSync } from "node:fs";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

import type { Store } from "./store.ts";
import type { FeedbackFile, StateFile } from "./types.ts";

/**
 * On-demand AnomalyGuessr generation (ticket #1210), the TypeScript
 * replacement for the Go handler in
 * core/platforms/api/dashboard_temporal_generate.go (deleted in #1242).
 *
 * The dev gallery's moderation queue runs dry eventually; POST /generate
 * starts one batch (pipeline/ag_generate.py) so Evan can top it up from the
 * page instead of waiting for the 03:15 cron, and GET /generate reports the
 * run's progress from the status file the script keeps.
 *
 * Cross-process safety works as before: the generator holds an
 * flock on data/anomalyguessr/generate.lock for its whole run (see
 * ag_generate.py's run lock), and a start is refused while that lock is held
 * (or while a batch this process spawned is still in flight), so a manual
 * click and the daily cron can never overlap. Without that, two generators
 * would read-modify-write state.json concurrently and lose each other's
 * scenes.
 */

/** One manual batch: one daily set (mirrors ag_queue.py's DAILY_COUNT). */
export const MANUAL_GENERATE_COUNT = 5;
/** Upper bound for the request body count, so a click cannot spend unbounded image calls. */
export const MAX_GENERATE_COUNT = 20;

const LOCK_NAME = "generate.lock";
const STATUS_NAME = "generate-status.json";

/** data/anomalyguessr/generate-status.json as pipeline/ag_generate.py writes it. */
export interface GenerateStatusFile {
  state?: string;
  pid?: number;
  count?: number;
  planned?: number;
  added?: number;
  failed?: number;
  imageCalls?: number;
  startedAt?: string;
  finishedAt?: string;
  updatedAt?: string;
  error?: string;
}

/**
 * GET/POST /generate payload: the last (or current) run's state plus the
 * queue's buffer depth. Fields the status file does not carry stay absent
 * from the JSON (undefined is dropped by JSON.stringify), like the Go
 * `omitempty` tags.
 */
export interface GenerateStatus {
  /** idle (nothing ran yet), running, done or error. */
  state: "idle" | "running" | "done" | "error";
  /** True while a generator (this API's or the cron's) holds the run lock. */
  running: boolean;
  /** Unshown accepted scenes: the fresh pool the next daily draws from. */
  buffer: number;
  count: number | undefined;
  planned: number;
  added: number;
  failed: number;
  imageCalls: number;
  startedAt: string | undefined;
  finishedAt: string | undefined;
  error: string | undefined;
}

/** Runs one batch to completion; tests inject a stub so no image money is spent. */
export type BatchRunner = (dataDir: string, count: number) => Promise<void>;

export interface GeneratorOptions {
  store: Store;
  /** Directory holding ag_generate.py: this repo's pipeline/ (since #1261). */
  pipelineDir: string;
  /** .env file with the model API keys (the fuchs repo's, not this public one). */
  envFile: string;
  /** Append-only generator log; the daily cron writes the same file. */
  logFile: string;
  runner?: BatchRunner;
}

/** A start was requested while a generation run is already in flight. */
export class GenerateBusyError extends Error {
  constructor() {
    super("a generation run is already in progress");
    this.name = "GenerateBusyError";
  }
}

/**
 * Owns the on-demand generator's API side: the in-flight flag of this
 * process (mirrors the Go server's tdGenRunning), the status file, and the
 * run lock probe. The lock is what covers the daily cron; the flag covers
 * the short window between this API spawning a child and the child taking
 * the lock.
 */
export class Generator {
  private readonly store: Store;
  private readonly pipelineDir: string;
  private readonly envFile: string;
  private readonly logFile: string;
  private readonly runner: BatchRunner;
  private running = false;

  constructor(opts: GeneratorOptions) {
    this.store = opts.store;
    this.pipelineDir = opts.pipelineDir;
    this.envFile = opts.envFile;
    this.logFile = opts.logFile;
    this.runner =
      opts.runner ??
      ((dir, count) =>
        spawnGenerator(
          this.pipelineDir,
          this.envFile,
          this.logFile,
          dir,
          count,
        ));
  }

  /** The UI-facing status: the generator's status file plus buffer depth. */
  async status(): Promise<GenerateStatus> {
    const dir = this.store.dir;
    // The lock probe is synchronous but the status file read is not, so a
    // run can finish (or start) in between. Probing on both sides of the
    // read keeps a just-finished run from being reported as "ended
    // unexpectedly" and a just-started one from looking dead.
    const runningBefore = this.runInFlight(dir);
    const file = await loadStatusFile(dir);
    const running = runningBefore || this.runInFlight(dir);
    // A file that says "running" while neither signal is live means the run
    // died without a final status; surface that as an error, not a stuck
    // spinner (the progress fields stay visible for context).
    let state: GenerateStatus["state"];
    let error = file.error;
    if (running) {
      state = "running";
    } else if (file.state === undefined || file.state === "") {
      state = "idle";
    } else if (file.state === "running") {
      state = "error";
      error = "generation run ended unexpectedly";
    } else {
      state = file.state === "done" ? "done" : "error";
    }
    return {
      state,
      running,
      buffer: await this.buffer(),
      count: file.count,
      planned: file.planned ?? 0,
      added: file.added ?? 0,
      failed: file.failed ?? 0,
      imageCalls: file.imageCalls ?? 0,
      startedAt: file.startedAt,
      finishedAt: file.finishedAt,
      error,
    };
  }

  /**
   * Kick off one batch and answer with the run's status. Throws
   * GenerateBusyError while a run is live; the route maps that to 409.
   */
  async start(count: number): Promise<GenerateStatus> {
    const dir = this.store.dir;
    // Checked and set synchronously (lockHeld is a sync probe), so two
    // concurrent starts cannot both pass this gate.
    if (this.running || lockHeld(dir)) throw new GenerateBusyError();
    this.running = true;
    try {
      // Reserve the run for the UI before the child writes its first
      // status, so a poll cannot read the previous run's result as current.
      await writeStatus(dir, {
        state: "running",
        count,
        startedAt: nowIso(),
      });
    } catch (err) {
      this.running = false;
      throw err;
    }
    void this.run(dir, count);
    return await this.status();
  }

  /** Unshown scenes a human accepted: the pool the next daily ship draws from. */
  private async buffer(): Promise<number> {
    const st = await this.store.state();
    const fb = await this.store.feedback();
    return unshownAccepted(st, fb);
  }

  private async run(dir: string, count: number): Promise<void> {
    try {
      await this.runner(dir, count);
    } catch (err) {
      await recordRunFailure(dir, count, err);
    } finally {
      this.running = false;
    }
  }

  /** True while a run is live: this process's flag or the run lock (the cron). */
  private runInFlight(dir: string): boolean {
    return this.running || lockHeld(dir);
  }
}

/** Scenes with no shown date that a human accepted (fresh daily pool). */
export function unshownAccepted(st: StateFile, fb: FeedbackFile): number {
  let n = 0;
  for (const e of Object.values(st.scenes)) {
    if (e.shown != null) continue;
    if (!(e.id in fb.accepted)) continue;
    if (e.id in fb.rejected) continue; // defensive: rejected wins over accepted
    n++;
  }
  return n;
}

/**
 * True when a generator process holds the run lock. `flock -n <lock> true`
 * takes the same advisory lock ag_generate.py takes for a whole run and
 * fails while the cron (or another API instance) holds it; util-linux flock
 * creates the lock file when missing, like the Go probe's O_CREATE.
 */
export function lockHeld(dataDir: string): boolean {
  if (!existsSync(dataDir)) return false; // no queue yet: nothing can hold it
  try {
    const probe = Bun.spawnSync(
      ["flock", "-n", path.join(dataDir, LOCK_NAME), "true"],
      { stdin: "ignore", stdout: "ignore", stderr: "ignore" },
    );
    return probe.exitCode !== 0;
  } catch {
    // No flock binary on PATH: the lock cannot be probed, so report free
    // (the status file still reports the run's own progress).
    return false;
  }
}

/** The generator's status file; a missing or unparseable file is empty. */
export async function loadStatusFile(
  dataDir: string,
): Promise<GenerateStatusFile> {
  try {
    const raw = await readFile(path.join(dataDir, STATUS_NAME), "utf8");
    return JSON.parse(raw) as GenerateStatusFile;
  } catch {
    return {};
  }
}

/** Atomic status write (tmp + rename, like the feedback writer), so the UI never reads half a file. */
export async function writeStatus(
  dataDir: string,
  status: GenerateStatusFile,
): Promise<void> {
  await mkdir(dataDir, { recursive: true });
  const file: GenerateStatusFile = { ...status, updatedAt: nowIso() };
  const tmp = path.join(
    dataDir,
    `.status-${Date.now()}-${Math.random().toString(36).slice(2)}.tmp`,
  );
  try {
    await writeFile(tmp, `${JSON.stringify(file, null, 2)}\n`);
    await rename(tmp, path.join(dataDir, STATUS_NAME));
  } catch (err) {
    await rmQuietly(tmp);
    throw err;
  }
}

/**
 * Make a runner-level failure visible in the UI. The script writes its own
 * terminal error status for failures inside the run, so this only fills the
 * gap when the run died before it could (a missing interpreter, a kill).
 */
async function recordRunFailure(
  dataDir: string,
  count: number,
  err: unknown,
): Promise<void> {
  const cur = await loadStatusFile(dataDir);
  if (cur.state === "done" || cur.state === "error") return;
  try {
    await writeStatus(dataDir, {
      state: "error",
      count,
      error: err instanceof Error ? err.message : String(err),
      finishedAt: nowIso(),
    });
  } catch (writeErr) {
    console.error("anomalyguessr generate: record failure:", writeErr);
  }
}

/**
 * Run pipeline/ag_generate.py detached and resolve when it exits. `detached`
 * puts the child in its own session (the Go Setsid), so a dashboard restart
 * cannot kill a run already spending image calls; the script's own run lock
 * serializes it against the daily cron. Output is appended to the log the
 * cron writes.
 */
export async function spawnGenerator(
  pipelineDir: string,
  envFile: string,
  logFile: string,
  dataDir: string,
  count: number,
): Promise<void> {
  const script = path.join(pipelineDir, "ag_generate.py");
  if (!existsSync(script)) {
    throw new Error(`generator script not found: ${script}`);
  }
  await mkdir(path.dirname(logFile), { recursive: true });
  const logFd = openSync(logFile, "a");
  const args = [
    script,
    "--data",
    dataDir,
    "--count",
    String(count),
    "--top-up",
  ];
  if (existsSync(envFile)) args.push("--env", envFile);
  try {
    const proc = Bun.spawn(["python3", ...args], {
      cwd: pipelineDir,
      stdin: "ignore",
      stdout: logFd,
      stderr: logFd,
      detached: true,
    });
    const code = await proc.exited;
    if (code !== 0) throw new Error(`generator exited with code ${code}`);
  } finally {
    closeSync(logFd);
  }
}

function nowIso(): string {
  return new Date().toISOString();
}

async function rmQuietly(p: string): Promise<void> {
  try {
    const { unlink } = await import("node:fs/promises");
    await unlink(p);
  } catch {
    // best-effort cleanup; the temp sits next to its target
  }
}
