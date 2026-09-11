import type { Manifest } from "../manifest";

/**
 * Single place where the frontend talks to the AnomalyGuessr backend.
 *
 * Ticket #1171 extracts the backend into an independent TypeScript service
 * that serves `/anomalyguessr/api/...` (manifest, scenes, moderate). That
 * service and its nginx location are not wired live yet, so the URLs below
 * stay on the scheme the dev instance serves TODAY: the manifest comes from
 * `scenes/manifest.json` (nginx proxies that path to the queue API on the dev
 * host, and GitHub Pages serves the static ship set), and moderation posts to
 * the existing fuchs2 route. Switching to #1171 is a change in this file
 * only; the game and queue call these functions, never raw URLs.
 */
const MANIFEST_URL = "scenes/manifest.json";

/** Moderation endpoint for the dev workflow (ticket #1163). */
function moderationUrl(sceneId: string): string {
  return `/api/v1/anomalyguessr/scenes/${sceneId}/moderate`;
}

/** Manifest JSON as served: a v1/v2 manifest plus the queue API's flag. */
type ManifestJson = Manifest & { moderation?: boolean };

/** Raw manifest payload: the parsed manifest plus the dev-instance flag. */
export interface LoadedManifest {
  manifest: Manifest;
  /** Queue API marks its payload with `moderation: true` (dev instance). */
  moderation: boolean;
}

/** Load the manifest; throws when the fetch fails. */
export async function loadManifest(
  parse: (raw: unknown) => Manifest,
): Promise<LoadedManifest> {
  const res = await fetch(MANIFEST_URL);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const raw = (await res.json()) as ManifestJson;
  // The queue API serializes an empty scene list as `null` (Go nil slice):
  // that is an empty queue, not a malformed manifest. Anything else is left
  // to the parser to accept or reject.
  const scenes = raw.scenes ?? [];
  return {
    manifest: parse({ ...raw, scenes }),
    moderation: raw.moderation === true,
  };
}

/** POST a moderation verdict (dev instance only); throws on a non-2xx. */
export async function postModeration(
  sceneId: string,
  action: "accept" | "reject",
  feedback: string,
): Promise<void> {
  const res = await fetch(moderationUrl(sceneId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, feedback }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

/** Dev queue generation, on demand (ticket #1210). */
const GENERATE_URL = "/api/v1/anomalyguessr/generate";

/** Status of the dev generator: buffer depth + the last/current run. */
export interface GenerateStatus {
  state: "idle" | "running" | "done" | "error";
  /** True while a run (this API's or the daily cron's) holds the run lock. */
  running: boolean;
  /** Unshown accepted scenes: the fresh pool the next daily draws from. */
  buffer: number;
  count: number;
  planned: number;
  added: number;
  failed: number;
  imageCalls: number;
  error?: string;
}

function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

/** Normalize the API payload; unknown states/values degrade to idle/0. */
function parseGenerateStatus(raw: unknown): GenerateStatus {
  const r = (raw ?? {}) as Record<string, unknown>;
  const state =
    r.state === "running" || r.state === "done" || r.state === "error"
      ? r.state
      : "idle";
  return {
    state,
    running: r.running === true || state === "running",
    buffer: num(r.buffer),
    count: num(r.count),
    planned: num(r.planned),
    added: num(r.added),
    failed: num(r.failed),
    imageCalls: num(r.imageCalls),
    ...(typeof r.error === "string" && r.error ? { error: r.error } : {}),
  };
}

async function generateError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: string; title?: string };
    if (body.detail) return body.detail;
    if (body.title) return body.title;
  } catch {
    // non-JSON error body: fall back to the status code
  }
  return `HTTP ${res.status}`;
}

/** Read the generator status (buffer depth + run progress). */
export async function loadGenerateStatus(): Promise<GenerateStatus> {
  const res = await fetch(GENERATE_URL);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return parseGenerateStatus(await res.json());
}

/** Start a generation run; a 409 (already running) throws its message. */
export async function startGenerate(): Promise<GenerateStatus> {
  const res = await fetch(GENERATE_URL, { method: "POST" });
  if (!res.ok) throw new Error(await generateError(res));
  return parseGenerateStatus(await res.json());
}
