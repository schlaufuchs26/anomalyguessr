import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import {
  type FeedbackFile,
  SCENE_ID_RE,
  type SceneEntry,
  type StateFile,
} from "./types.ts";

/**
 * Data access for the AnomalyGuessr queue. One instance owns the data dir
 * (default <repo>/data/anomalyguessr) and reads state.json
 * (written by the pipeline) + feedback.json (written here). Reads are done
 * per-request (the pipeline rewrites state.json out from under us); writes
 * to feedback.json are serialized via a mutex and done atomically (tmp +
 * rename), mirroring ag_queue.py's save_state, so two quick clicks cannot
 * lose an update.
 */
export class Store {
  readonly dir: string;
  private writeLock: Promise<void> = Promise.resolve();

  constructor(dir: string) {
    this.dir = dir;
  }

  async state(): Promise<StateFile> {
    return loadJson<StateFile>(path.join(this.dir, "state.json"), {
      version: 1,
      last_shipped: null,
      scenes: {},
    });
  }

  async feedback(): Promise<FeedbackFile> {
    return normalizeFeedback(
      await loadJson<FeedbackFile>(path.join(this.dir, "feedback.json"), {
        version: 1,
        rejected: {},
        accepted: {},
        comments: {},
      }),
    );
  }

  /** Serialized, atomic feedback.json write (single-flight mutex). */
  async saveFeedback(fb: FeedbackFile): Promise<void> {
    await mkdir(this.dir, { recursive: true });
    const run = this.writeLock.then(async () => {
      const tmp = path.join(
        this.dir,
        `.feedback-${Date.now()}-${Math.random().toString(36).slice(2)}.tmp`,
      );
      try {
        await writeFile(tmp, `${JSON.stringify(fb, null, 2)}\n`);
        await rename(tmp, path.join(this.dir, "feedback.json"));
      } catch (err) {
        try {
          await import("node:fs/promises").then((m) => m.unlink(tmp));
        } catch {
          // best-effort cleanup; the temp is on the same dir as the target
        }
        throw err;
      }
    });
    this.writeLock = run.catch(() => {});
    return run;
  }
}

async function loadJson<T>(p: string, fallback: T): Promise<T> {
  try {
    const raw = await readFile(p, "utf8");
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

/**
 * Fill in missing map fields so downstream code can index safely, and fold
 * the pre-#1207 legacy "excluded" map into `rejected` (dropping the old key:
 * the next save writes only the new name).
 */
export function normalizeFeedback(fb: FeedbackFile): FeedbackFile {
  return {
    version: fb.version ?? 1,
    rejected: { ...(fb.excluded ?? {}), ...(fb.rejected ?? {}) },
    accepted: fb.accepted ?? {},
    comments: fb.comments ?? {},
  };
}

export function normalizeState(st: StateFile): StateFile {
  return { ...st, scenes: st.scenes ?? {} };
}

/** A scene exists in state.json and passes the safe-slug rule. */
export function sceneExists(st: StateFile, id: string): boolean {
  return SCENE_ID_RE.test(id) && id in st.scenes;
}

/** Derive a scene's moderation verdict from feedback.json (rejected wins). */
export function moderationOf(
  fb: FeedbackFile,
  id: string,
): "accepted" | "rejected" | "unmoderated" {
  if (id in fb.rejected) return "rejected";
  if (id in fb.accepted) return "accepted";
  return "unmoderated";
}

/** Scene entries sorted newest-added first (then id), for the queue list. */
export function sortedEntries(st: StateFile): SceneEntry[] {
  return Object.values(st.scenes).sort(
    (a, b) => b.added.localeCompare(a.added) || a.id.localeCompare(b.id),
  );
}
