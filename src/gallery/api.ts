// API payload types and display helpers shared by the gallery components.
// The types mirror this repo's backend payloads (api/src/types.ts); the
// gallery is its own entry build, so they stay local instead of importing
// across the api/ package boundary.

export interface TDComment {
  text: string;
  createdAt: string;
}
/** The last checker verdict stored with a scene (ticket #1436): requirements
 *  met (0..8, 8 = all met), the failed requirement numbers and the checker's
 *  reason. */
export interface SceneChecker {
  score: number;
  failed: number[];
  /** Requirements the score is out of (ticket #1476). Absent on a verdict
   *  from the older eight-requirement rubric, which then reads as /8. */
  total?: number;
  /** Soft-point count (ticket #1502): one point per met soft criterion.
   *  Absent on verdicts stored before #1502. */
  points?: number;
  /** Soft criteria in the verdict's rubric (5 on the nine-requirement one). */
  points_total?: number;
  reason: string;
}
/** "checker 5/9" plus the failed numbers, e.g. "checker 5/9 · failed 3, 7".
 *  The denominator is the verdict's own total (ag_generate.REQUIREMENTS,
 *  nine since #1476); a verdict stored before that carries no total and
 *  reads as the old eight-requirement rubric. */
export function checkerLabel(checker: SceneChecker): string {
  const failed = checker.failed.length
    ? ` · failed ${checker.failed.join(", ")}`
    : "";
  return `checker ${checker.score}/${checker.total ?? 8}${failed}`;
}

/** "points 3/5" (ticket #1502): the soft-point pot, separate from the hard
 *  mechanical defects. Null when the verdict carries no point count. */
export function pointsLabel(checker: SceneChecker): string | null {
  if (checker.points === undefined || checker.points_total === undefined) {
    return null;
  }
  return `points ${checker.points}/${checker.points_total}`;
}

/** The hard mechanical defects of a render as display labels (#1502); the
 *  card renders them as a warning line. Unknown keys are ignored. */
const DEFECT_LABELS: Record<string, string> = {
  presence: "element missing or outside the click area",
  tone: "color on a grayscale source",
  size: "size out of band",
};

export function defectLabels(
  mechanical: Record<string, unknown> | undefined | null,
): string[] {
  if (!mechanical) return [];
  return Object.entries(DEFECT_LABELS)
    .filter(([key]) => mechanical[key] === true)
    .map(([, label]) => label);
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
  /** The scene shipped with an unrepaired checker finding (ticket #1449);
   *  the card flags it so moderation looks at it first. Only while the scene
   *  is still undecided (ticket #1532). */
  needsReview?: boolean;
  /** Where the shown title came from (ticket #1496): the source catalogue
   *  name, or the "Photograph" placeholder. Absent on older scenes. */
  titleSource?: "catalog" | "fallback";
  /** Hard mechanical defects flagged on the shipped render (ticket #1502);
   *  the card renders them as a warning line. Absent on older scenes. */
  mechanical?: Record<string, unknown> | null;
  /** The pre-fix click ellipse when the presence recompute moved it (ticket
   *  #1504); the lightbox draws it as a dashed "before". Absent otherwise. */
  answerBefore?: AnswerCircle | null;
  /** The optional "lustig" moderation tag (ticket #1502). */
  funny: boolean;
  /** When the tag was set, when it is (RFC3339). */
  funnyAt?: string | null;
  /** The optional "great" curation tag (ticket #1541). */
  great: boolean;
  /** When the tag was set, when it is (RFC3339). */
  greatAt?: string | null;
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

/** Whether the card and the lightbox show the "needs review" chip (ticket
 *  #1532). The flag marks a scene whose checks did not pass cleanly, which is
 *  only actionable while no accept/reject decision exists; the API already
 *  suppresses it on decided scenes, and this guard keeps a payload from an
 *  older server from re-introducing a badge nobody can act on. */
export function showsNeedsReview(
  scene: Pick<TDScene, "needsReview" | "moderation">,
): boolean {
  return scene.needsReview === true && scene.moderation === "unmoderated";
}

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

/** Order the rejected view by rejection time, newest first (ticket #1475).
 *
 *  The API sorts its one scene list by `added` desc and serves it to every
 *  filter (and to the game's own views), so the ordering belongs here, per
 *  chip: only the rejected view wants moderation time. A scene whose
 *  `rejectedAt` is missing or unparseable sorts last (legacy rejections from
 *  before the timestamp was written), tie-broken by the API's own rule
 *  (`added` desc, then id). */
export function sortRejectedScenes(scenes: TDScene[]): TDScene[] {
  const rejectedTime = (s: TDScene): number | null => {
    if (!s.rejectedAt) return null;
    const t = Date.parse(s.rejectedAt);
    return Number.isNaN(t) ? null : t;
  };
  return [...scenes].sort((a, b) => {
    const ta = rejectedTime(a);
    const tb = rejectedTime(b);
    if (ta !== null && tb !== null) {
      if (ta !== tb) return tb - ta;
    } else if (ta !== null) {
      return -1;
    } else if (tb !== null) {
      return 1;
    }
    return b.added.localeCompare(a.added) || a.id.localeCompare(b.id);
  });
}

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
  /** Checker stages: requirements met, the failed numbers and the rubric
   *  size the score is out of (#1476; absent = the old eight-requirement
   *  rubric). */
  score?: number | null;
  failed?: number[];
  total?: number;
  /** Checker stages: the checker's one-line reason (ticket #1446). */
  reason?: string;
  /** Mechanical-gate rejection of a draw (replaces the answer). */
  rejected?: string;
  /** Image-edit steps: the seed used for the draw. */
  seed?: number;
}

/** The score guard's record (ticket #1461): the pipeline ships the
 *  best-scoring render of the correction chain, not always the newest one.
 *  Written only when that is an earlier round; the newest round it passed
 *  over is `rejected`. */
export interface ScoreGuard {
  /** Round whose render shipped (0 = the initial draw). */
  shipped: number;
  /** What the checker gave that round (null when unreadable), and the
   *  rubric size it is out of (#1476). */
  score: number | null;
  total?: number;
  /** The newest round the guard passed over. */
  rejected?: { round: number; score: number | null; total?: number };
}

export interface SceneTrace {
  scene?: string;
  source?: string;
  date?: string;
  model?: string;
  image_model?: string;
  recordedAt?: string;
  calls: TraceStep[];
  /** Which round's render shipped, when it is not the last one (ticket
   *  #1461). Absent on traces of scenes whose last round shipped. */
  score_guard?: ScoreGuard;
  /** The sampling mode that produced the scene (ticket #1497). */
  sampling_mode?: string;
  /** Draw number whose render shipped in the independent mode (#1497). */
  selected_draw?: number;
  /** One row per fresh render in the independent mode (#1497). Absent on
   *  repair-chain traces and on scenes generated before the mode existed. */
  draws?: DrawRow[];
  /** The mechanical checks of the shipped render (ticket #1485): presence,
   *  tone and size, plus the click-area recompute of ticket #1504. */
  mechanical_checks?: Record<string, unknown> | null;
  /** The last attempt's failure reason, when the scene still landed. */
  error?: string;
  gate_failures?: { attempt?: number; reason?: string }[];
  call_errors?: { stage?: string; error?: string; after_fix?: boolean }[];
}

/** One independent draw of a scene (ticket #1497): a fresh render of the
 *  source photo with its own checker verdict and mechanical finding. The
 *  pipeline writes one row per draw and names the shipped one in
 *  `selected_draw`. */
interface DrawRow {
  draw: number;
  image?: string;
  seed?: number;
  model?: string;
  duration_s?: number;
  cost?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  score: number | null;
  failed: number[];
  mechanical?: Record<string, unknown> | null;
  clean: boolean;
  shipped: boolean;
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
