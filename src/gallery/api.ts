// API payload types and display helpers shared by the gallery components.
// The types mirror this repo's backend payloads (api/src/types.ts); the
// gallery is its own entry build, so they stay local instead of importing
// across the api/ package boundary.

export interface TDComment {
  text: string;
  createdAt: string;
}
/** The last checker verdict stored with a scene (ticket #1436): requirements
 *  met (0..7), the failed requirement numbers and the checker's reason. */
export interface SceneChecker {
  score: number;
  failed: number[];
  reason: string;
}
/** "checker 5/8" plus the failed numbers, e.g. "checker 5/8 · failed 3, 7".
 *  Eight is the checker's requirement count (ag_generate.REQUIREMENTS). */
export function checkerLabel(checker: SceneChecker): string {
  const failed = checker.failed.length
    ? ` · failed ${checker.failed.join(", ")}`
    : "";
  return `checker ${checker.score}/8${failed}`;
}
/** Answer circle in normalized image coordinates (0..1, top-left origin):
 *  x/y center, r radius. The game scores a click against exactly this
 *  circle (scoring.ts isHit); the gallery draws it (ticket #1209). */
export interface AnswerCircle {
  x: number;
  y: number;
  r: number;
}
interface TDSceneImages {
  edited: string;
  original: string;
  audit?: string;
}
export interface TDScene {
  id: string;
  /** Short, speakable handle ("AG-137", ticket #1413); the long `id` stays
   *  the canonical key for files, URLs and trace sidecars. */
  shortId?: string;
  title: string;
  place: string;
  year: string;
  credit?: string;
  sourceUrl?: string;
  anomaly: string;
  description?: string;
  source: Record<string, unknown>;
  answer?: AnswerCircle | null;
  hints: string[];
  added: string;
  shown: string | null;
  state: "unshown" | "shown" | "rejected";
  rejected: boolean;
  rejectedAt?: string | null;
  moderation: "accepted" | "rejected" | "unmoderated";
  comments: TDComment[];
  /** A generation trace sidecar exists (ticket #1373); the modal then
   *  offers the collapsible "Generation trace" panel. Scenes generated
   *  before the trace existed carry false and show "no trace recorded". */
  hasTrace: boolean;
  /** Last checker verdict (ticket #1436); absent on scenes generated without
   *  the checker. The card and the lightbox show it before any image opens. */
  checker?: SceneChecker;
  images: TDSceneImages;
}
export interface TDList {
  summary: {
    total: number;
    unshown: number;
    shown: number;
    accepted: number;
    rejected: number;
    unmoderated: number;
  };
  scenes: TDScene[];
  /** Ship-eligible scene ids in the pipeline's daily pick order (#1208);
   *  the first dailyCount are the upcoming daily set. */
  dailyOrder: string[];
  /** Size of one daily set (mirrors pipeline/ag_queue.py DAILY_COUNT). */
  dailyCount: number;
}

export const QUERY_KEY = ["td-scenes"];

/**
 * Base path of the AnomalyGuessr backend: the TypeScript service
 * (the game repo's `api/`) behind nginx's `/anomalyguessr/api/`
 * location (ticket #1242). The Go copy that used to serve
 * `/api/v1/anomalyguessr/...` from fuchs2 is gone.
 */
const AG_API = "/anomalyguessr/api";

/** Build an AnomalyGuessr API URL from a path relative to the base. */
export function agUrl(path: string): string {
  return `${AG_API}/${path.replace(/^\/+/, "")}`;
}

export async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as T;
}

/** "2026-09-08" -> "08.09.2026" (added/shown dates are date-only strings). */
export function fmtDate(iso: string): string {
  const [y, m, d] = iso.split("-");
  return `${d}.${m}.${y}`;
}

export function fmtDateTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("de-DE", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export const MOD_LABELS: Record<TDScene["moderation"], string> = {
  accepted: "Accepted",
  rejected: "Rejected",
  unmoderated: "Unmoderated",
};

// One user-facing vocabulary: the moderation axis (#1205). The ship state
// (`state`/`shown` in the API payload) stays data; the card keeps the "shown"
// date in its meta line, but no Unshown/Shown/Rejected badge or chip.
//
// "daily" is the landing view (#1208): the accepted scenes in the pipeline's
// own pick order (from the API's dailyOrder), i.e. what the daily will show
// next, with the leading dailyCount cards marked. Rejected and unmoderated
// scenes keep their own chips.
export type Filter = "daily" | "rejected" | "unmoderated";

export const FILTER_LABELS: Record<Filter, string> = {
  daily: "Daily",
  rejected: "Rejected",
  unmoderated: "Unmoderated",
};

// ── Generation traces (ticket #1373) ──────────────────────────────────────
//
// One JSON sidecar per scene, written by the Python pipeline as it runs
// (`data/anomalyguessr/traces/<scene-id>.json`) and served by the API at
// GET /anomalyguessr/api/traces/{id}. The gallery fetches it only when the
// panel is opened, so the scene list stays one small payload.

export interface TraceUsage {
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens?: number;
  cost?: number;
}

/** One pipeline step (LLM call or image edit) in the scene's trace. */
export interface TraceStep {
  /** proposal | edit r0 | check r0 | fix-edit r1 | check r1 | coordinates |
   *  click-target 1 | ... (ticket #1436 names the edit/check rounds). */
  stage: string;
  /** Which attempt of the scene produced this step (1-based). */
  attempt?: number;
  /** Round of the chain: 0 = initial draw, 1/2 = correction rounds. */
  round?: number;
  /** Which mechanical retry of the round drew this image (1-based). */
  draw?: number;
  model?: string;
  prompt?: string;
  answer?: string;
  /** Hidden chain-of-thought when the model returned one. */
  reasoning?: string;
  /** The image this step saw: file name or attempt output, no base64. */
  image?: string;
  /** ISO timestamp the call started. */
  at?: string;
  usage?: TraceUsage;
  duration_s?: number;
  /** Set when the call failed (the step then carries no answer). */
  error?: string;
  /** True on the second coordinates call after a correction edit. */
  after_fix?: boolean;
  /** Click-target steps: the answer area that was judged. */
  judged?: { x: number; y: number; r: number };
  /** Click-target steps: the anomaly box the model returned (#1445). */
  box?: { x1: number; y1: number; x2: number; y2: number } | null;
  /** Click-target steps: the corrected answer, null when none. */
  corrected?: { x: number; y: number; r: number } | null;
  /** Click-target steps: whether the drawn area covered the anomaly box. */
  covers?: boolean | null;
  /** Click-target steps: the model's one-line reason. */
  verdict_reason?: string;
  /** Checker stages: requirements met (0..7) and the failed numbers. */
  score?: number | null;
  failed?: number[];
  /** Mechanical-gate rejection of a draw (replaces the answer). */
  rejected?: string;
  /** Image-edit steps: the seed used for the draw. */
  seed?: number;
}

export interface SceneTrace {
  scene?: string;
  source?: string;
  date?: string;
  model?: string;
  image_model?: string;
  recordedAt?: string;
  calls: TraceStep[];
  /** The last attempt's failure reason, when the scene still landed. */
  error?: string;
  gate_failures?: { attempt?: number; reason?: string }[];
  call_errors?: { stage?: string; error?: string; after_fix?: boolean }[];
}

/** Fetch one scene's generation trace (the panel calls this on open). */
export function getSceneTrace(id: string): Promise<SceneTrace> {
  return getJSON<SceneTrace>(agUrl(`traces/${id}`));
}

/** "3.2s" / "820ms"; the step durations are seconds as a float. */
export function fmtDuration(seconds?: number): string {
  if (seconds === undefined || seconds === null) return "";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  return `${seconds.toFixed(1)}s`;
}

/** "900+120 tok · $0.0003", skipping zero parts. */
export function fmtUsage(usage?: TraceUsage): string {
  if (!usage) return "";
  const parts: string[] = [];
  const tokens = usage.prompt_tokens + usage.completion_tokens;
  if (tokens > 0)
    parts.push(
      `${tokens} tok` +
        (usage.reasoning_tokens
          ? ` (${usage.reasoning_tokens} reasoning)`
          : ""),
    );
  if (usage.cost) parts.push(`$${usage.cost.toFixed(4)}`);
  return parts.join(" · ");
}
