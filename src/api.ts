import type { Manifest } from "../manifest";
import type { TraceStep } from "./gallery/api";

/**
 * Single place where the frontend talks to the AnomalyGuessr backend.
 *
 * The backend is the TypeScript service
 * `services/anomalyguessr-api`, served behind nginx at `/anomalyguessr/api/`
 * (ticket #1242; the Go copy in fuchs2 is gone). Manifest URLs stay relative
 * on purpose: the dev host proxies `scenes/manifest.json` to the service,
 * while GitHub Pages serves the static ship set from the same path, so one
 * build keeps working on both hosts. The dev-only calls (moderation,
 * generate) use the absolute API prefix.
 */
const MANIFEST_URL = "scenes/manifest.json";

/**
 * The dev instance's day set (ticket #1221), fetched instead of the raw queue
 * for the Daily mode. nginx proxies it to the queue API's scope=daily, i.e.
 * the set ship() would pick (accepted, fresh before recycled, variety
 * preferred). Prod never fetches it: its static scenes/manifest.json already
 * is the day's set.
 */
const DAILY_MANIFEST_URL = "scenes/daily.json";

/**
 * The dev instance's Live set (ticket #1237), fetched for the Live mode: the
 * scenes prod is serving right now (the queue API's scope=live, i.e. the set
 * the last ship wrote). The Daily mode previews the next set to ship; Live
 * replays the live one. Prod never fetches it: its static
 * scenes/manifest.json already is the live set.
 */
const LIVE_MANIFEST_URL = "scenes/live.json";

/** Moderation endpoint for the dev workflow (ticket #1163). */
function moderationUrl(sceneId: string): string {
  return `/anomalyguessr/api/scenes/${sceneId}/moderate`;
}

/** The optional "lustig" tag endpoint (#1502). */
function funnyUrl(sceneId: string): string {
  return `/anomalyguessr/api/scenes/${sceneId}/funny`;
}

/** Manifest JSON as served: a v1/v2 manifest plus the queue API's flag. */
type ManifestJson = Manifest & { moderation?: boolean };

/** Raw manifest payload: the parsed manifest plus the dev-instance flag. */
export interface LoadedManifest {
  manifest: Manifest;
  /** Queue API marks its payload with `moderation: true` (dev instance). */
  moderation: boolean;
}

/** Load a manifest URL; throws when the fetch fails. */
async function loadManifestFrom(
  url: string,
  parse: (raw: unknown) => Manifest,
): Promise<LoadedManifest> {
  const res = await fetch(url);
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

/** Load the game's manifest; throws when the fetch fails. */
export async function loadManifest(
  parse: (raw: unknown) => Manifest,
): Promise<LoadedManifest> {
  return loadManifestFrom(MANIFEST_URL, parse);
}

/**
 * Load the dev instance's day set for the Daily mode (ticket #1221); throws
 * when the fetch fails. Only called when the manifest carries the dev flag;
 * on prod the plain manifest already is the day's set.
 */
export async function loadDailyManifest(
  parse: (raw: unknown) => Manifest,
): Promise<Manifest> {
  return (await loadManifestFrom(DAILY_MANIFEST_URL, parse)).manifest;
}

/**
 * Load the dev instance's Live set for the Live mode (ticket #1237); throws
 * when the fetch fails, and the caller then offers no Live mode at all
 * (playing the queue under a "Live" label would be a lie). Only called when
 * the manifest carries the dev flag.
 */
export async function loadLiveManifest(
  parse: (raw: unknown) => Manifest,
): Promise<Manifest> {
  return (await loadManifestFrom(LIVE_MANIFEST_URL, parse)).manifest;
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

/**
 * POST the optional "lustig" tag (dev instance only, ticket #1502); returns
 * the server's resulting state. Independent of the accept/reject verdict.
 */
export async function postFunny(
  sceneId: string,
  tag: boolean,
): Promise<boolean> {
  const res = await fetch(funnyUrl(sceneId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tag }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const body = (await res.json()) as { funny?: boolean };
  return body.funny === true;
}

/** Dev queue generation, on demand (ticket #1210). */
const GENERATE_URL = "/anomalyguessr/api/generate";

/**
 * The trace of the scene a run is working on (ticket #1446), as the poll of
 * GET /generate carries it: step metadata only. The large text fields
 * (prompt/answer/reasoning) are not part of this payload; opening a step
 * fetches them from `/anomalyguessr/api/traces/{ref}`.
 */
export interface LiveTrace {
  /** The in-flight trace's slug; `/traces/{ref}` serves its full text. */
  ref: string;
  /** Steps in order (see TraceStep; the text fields are absent). */
  steps: TraceStep[];
  /** The pipeline's own note on the current attempt's failure, when any. */
  error?: string;
}

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
  /** Pipeline phase of the current scene (#1381). */
  phase?: string;
  /** Source id of the scene being worked on. */
  scene?: string;
  sceneIndex?: number;
  scenesTotal?: number;
  /** Round of the current scene (#1436): 0 = initial draw, 1/2 = correction. */
  round?: number;
  /** Mechanical retry of the current round being drawn, 1-based. */
  draw?: number;
  /** Click-target pass of the current scene (#1446), 1-based. */
  clickPass?: number;
  /** Long id of the last scene that landed (#1446); its finished trace
   *  sidecar is the one the live view opens after the run. */
  sceneId?: string;
  /** Running cost in USD as the generator reports it. */
  cost?: number;
  /** The running scene's trace steps (#1446); absent when none is active. */
  liveTrace?: LiveTrace;
  error?: string;
}

function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

/** An object counts as a trace step when it names a stage. */
function isStep(v: unknown): v is TraceStep {
  return (
    typeof v === "object" &&
    v !== null &&
    typeof (v as { stage?: unknown }).stage === "string"
  );
}

/** Parse the poll's live trace; anything malformed drops to no live trace. */
function parseLiveTrace(raw: unknown): LiveTrace | undefined {
  if (typeof raw !== "object" || raw === null) return undefined;
  const r = raw as Record<string, unknown>;
  if (typeof r.ref !== "string" || r.ref === "") return undefined;
  const steps = Array.isArray(r.steps) ? r.steps.filter(isStep) : [];
  return {
    ref: r.ref,
    steps,
    ...(typeof r.error === "string" && r.error ? { error: r.error } : {}),
  };
}

/** Normalize the API payload; unknown states/values degrade to idle/0. */
function parseGenerateStatus(raw: unknown): GenerateStatus {
  const r = (raw ?? {}) as Record<string, unknown>;
  const state =
    r.state === "running" || r.state === "done" || r.state === "error"
      ? r.state
      : "idle";
  const liveTrace = parseLiveTrace(r.liveTrace);
  return {
    state,
    running: r.running === true || state === "running",
    buffer: num(r.buffer),
    count: num(r.count),
    planned: num(r.planned),
    added: num(r.added),
    failed: num(r.failed),
    imageCalls: num(r.imageCalls),
    ...(typeof r.phase === "string" && r.phase ? { phase: r.phase } : {}),
    ...(typeof r.scene === "string" && r.scene ? { scene: r.scene } : {}),
    sceneIndex: num(r.sceneIndex),
    scenesTotal: num(r.scenesTotal),
    round: num(r.round),
    draw: num(r.draw),
    clickPass: num(r.clickPass),
    ...(typeof r.sceneId === "string" && r.sceneId
      ? { sceneId: r.sceneId }
      : {}),
    cost: num(r.cost),
    ...(liveTrace ? { liveTrace } : {}),
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
