/**
 * AnomalyGuessr backend types: the Python pipeline's
 * data/anomalyguessr/state.json + feedback.json shapes, plus the API payloads
 * the gallery consumes. (The Go copy this service mirrored,
 * core/platforms/api/dashboard_temporal.go, was deleted in #1242.)
 *
 * The backend owns data/anomalyguessr/ (the single source of truth shared
 * with the ag_queue.py/ag_verify.py pipeline scripts). state.json is written
 * by ag_queue.py (read-only here); feedback.json is written by this API only
 * (ag_queue.py reads it read-only). Keeping the split writer ownership
 * (documented in wiki/entries/anomalyguessr-queue.md) avoids lost updates.
 */

import type { RejectReason } from "../../rejectReasons.ts";

/** The last checker verdict the generator stored with a scene (#1436). */
export interface SceneChecker {
  /** Requirements met, computed from the failed numbers. */
  score: number;
  /** Numbers of the requirements the shipped image failed. */
  failed: number[];
  /** Rubric size the score is out of (#1476; absent on verdicts from the
   *  older eight-requirement rubric). */
  total?: number;
  /** Soft-point count (#1502): one point per met soft criterion, the second
   *  pot next to the hard mechanical defects. Absent on verdicts stored
   *  before #1502. */
  points?: number;
  /** Soft criteria in the verdict's rubric (5 on the nine-requirement one). */
  points_total?: number;
  /** The checker's one-line reason for that verdict. */
  reason: string;
}

/** One queue scene as stored in state.json (a manifest scene minus image paths, plus added/shown). */
export interface SceneEntry {
  id: string;
  /**
   * Short, speakable handle ("AG-137", ticket #1413): assigned by the
   * pipeline's ag_queue.py at queue-add time and never renumbered. The long
   * `id` stays the canonical key for files, URLs and trace sidecars.
   */
  shortId?: string;
  title: string;
  place: string;
  year: string;
  credit: string;
  sourceUrl: string;
  source?: Record<string, unknown>;
  anomaly: string;
  /** Variety key written at generation time (ticket #1232); absent on old entries. */
  family?: string;
  description: string;
  answer: { x: number; y: number; r: number };
  /**
   * Legacy progressive hints. Removed from the game in ticket #1407; stored
   * scenes may still carry the field, so it stays readable and is ignored
   * (never rendered into a manifest).
   */
  hints?: string[];
  /**
   * Last checker verdict (ticket #1436): the gallery card and lightbox show
   * it without opening an image. Absent on scenes generated without the
   * checker, and never written into the public manifest.
   */
  checker?: SceneChecker;
  /**
   * Moderation flag (ticket #1449): the pipeline shipped the scene without
   * a full repair (a requirement-8 finding, or a checker repair that asked
   * for another object). Dev-side curation metadata; never in the manifest.
   */
  needs_review?: boolean;
  /**
   * Where the shown title came from (ticket #1496): the source's own
   * catalogue name ("catalog") or the "Photograph" placeholder ("fallback").
   * Absent on scenes queued before the field existed.
   */
  title_source?: "catalog" | "fallback";
  /**
   * The hard mechanical defects of the shipped render (ticket #1502):
   * booleans for presence, tone and size from ag_checks. Never offset by the
   * soft checker points; the gallery shows them as a warning line.
   */
  mechanical?: { presence?: boolean; tone?: boolean; size?: boolean };
  /**
   * The pre-fix click ellipse when the presence recompute moved it (ticket
   * #1504): the lightbox draws it as a dashed "before" next to the shipped
   * answer. Dev-side curation metadata; never in the public manifest.
   */
  answer_before?: { x: number; y: number; r: number };
  added: string;
  shown: string | null;
  explanation?: string;
  references?: { label: string; url: string }[];
}

export interface StateFile {
  version: number;
  last_shipped: string | null;
  /**
   * The shipped day's manifest order (ticket #1237), written by
   * ag_queue.py ship(). Absent on days shipped before the field existed;
   * manifestLive falls back to an (added, id) order then.
   */
  last_shipped_ids?: string[];
  /**
   * High-water mark of the short-handle serials (ticket #1413), written by
   * ag_queue.py; the API only reads it.
   */
  next_short?: number;
  scenes: Record<string, SceneEntry>;
}

export interface Comment {
  text: string;
  createdAt: string;
}

/** One canonical rejection reason stored on a scene (ticket #1627). */
export interface RejectReasonEntry {
  /**
   * The canonical reasons in `rejectReasons.ts` (REJECT_REASONS). A row
   * written before #2585 carries a single string; from #2585 on it is a
   * list, so several reasons survive one decision. Readers pass it through
   * `entryReasons()` and see a list either way.
   */
  reason: RejectReason | RejectReason[];
  /** When the reason was recorded (RFC3339). */
  at: string;
}

/**
 * One "Needs work" repair request and its outcome (ticket #2590).
 *
 * The moderation view's third button stores the ticked reasons here and
 * leaves the scene's accept/reject verdict alone. While `status` is
 * "requested" the scene is out of the moderation queue (its moderation
 * reads "repair"); the heartbeat's repair pass then either fixes what the
 * reasons point at and puts the scene back as unmoderated ("repaired") or
 * names the reason that cannot be fixed and returns it to rejected
 * ("unrepairable"). The request reasons and, when repaired, what changed
 * stay here, so a scene's old and new reasons are both traceable.
 */
export interface RepairEntry {
  status: "requested" | "repaired" | "unrepairable";
  /** The canonical reasons ticked at request time (the old reasons). */
  reasons: RejectReason[];
  /** When the request was stored (RFC3339). */
  requestedAt: string;
  /** When the repair pass resolved it (RFC3339); absent while requested. */
  resolvedAt?: string;
  /** What the repair targeted: the click area, the text, both, or nothing. */
  kind?: "click" | "text" | "click+text" | "none";
  /** The repair pass's one-line account (what changed / why not fixable). */
  detail?: string;
  /** The answer circle before the click-area repair. */
  answerBefore?: { x: number; y: number; r: number };
  /** The answer circle the repair wrote back. */
  answerAfter?: { x: number; y: number; r: number };
  /** The explanation before the text repair. */
  textBefore?: string;
  /** The explanation the repair wrote back. */
  textAfter?: string;
}

/** One scene's human quality rating (ticket #2605). */
export interface RatingEntry {
  /** The rating on the 0-10 scale (RATING_MIN..RATING_MAX). */
  value: number;
  /** When the rating was last set (RFC3339). */
  at: string;
}

export interface FeedbackFile {
  version: number;
  /** Scene id -> RFC3339 timestamp (written by this API). */
  rejected: Record<string, string>;
  /** Pre-#1207 legacy name of `rejected`; normalizeFeedback folds it in. */
  excluded?: Record<string, string>;
  accepted: Record<string, string>;
  comments: Record<string, Comment[]>;
  /**
   * The canonical rejection reason per scene (ticket #1627): scene id ->
   * {reason, at}. Written when a reject uses one of the five buttons; a
   * legacy rejection, or one with free prose only, has no entry here.
   */
  reasons: Record<string, RejectReasonEntry>;
  /**
   * The optional "funny" moderation tag (ticket #1502): scene id -> RFC3339
   * timestamp. The labelled set the blind vision test needs before "funny"
   * can become a point. Independent of the accept/reject verdict.
   */
  funny?: Record<string, string>;
  /**
   * The human quality rating per scene (ticket #2605): scene id ->
   * {value, at}. Replaces the "great" tick; a scene rated at least
   * RATING_HIGH counts as a positive example (the pattern catalogue, the
   * feedback pass and the daily ship rule read it that way). Independent of
   * the accept/reject verdict.
   */
  rating?: Record<string, RatingEntry>;
  /**
   * Legacy "great" tick (ticket #1541), kept readable as a migration
   * fallback: normalizeFeedback folds every id here into `rating` at value
   * RATING_MAX, and the next write drops the key. Never written anew.
   */
  great?: Record<string, string>;
  /**
   * "Needs work" repair requests and their outcomes (ticket #2590): scene
   * id -> RepairEntry. A pending request (status "requested") takes the
   * scene out of the moderation queue; the repair pass resolves it.
   */
  repairs?: Record<string, RepairEntry>;
}

/**
 * The remaining moderation tag beside the rating (ticket #2605): "great"
 * became the 0-10 rating, so only "funny" is still a plain tag.
 */
export const MODERATION_TAGS = ["funny"] as const;
export type ModerationTag = (typeof MODERATION_TAGS)[number];

/** The rating scale (ticket #2605): a whole number from 0 to 10. */
export const RATING_MIN = 0;
export const RATING_MAX = 10;
/**
 * A scene rated at least this counts as a positive example (ticket #2605):
 * the daily ship rule swaps one such scene into the day, and the feedback
 * pass promotes the patterns behind them.
 */
export const RATING_HIGH = 8;

/** Whether `value` is a whole number on the rating scale. */
export function isRatingValue(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= RATING_MIN &&
    value <= RATING_MAX
  );
}

/**
 * A scene's curation state. "repair" (ticket #2590) is a scene with an open
 * "Needs work" request: it is neither accepted nor rejected, and it stays
 * out of the moderation queue until the repair pass resolves it.
 */
export type Moderation = "accepted" | "rejected" | "unmoderated" | "repair";

/** The scene-id slug rule (same as ag_queue.py + the old Go handler). */
export const SCENE_ID_RE = /^[a-z0-9][a-z0-9-]*$/;
/**
 * The short-handle rule (ticket #1413), mirroring ag_queue.SHORT_ID_RE.
 * Case-insensitive: a hand-typed "ag-137" resolves to the same scene.
 */
export const SCENE_HANDLE_RE = /^AG-\d{1,6}$/i;
export const MAX_COMMENT = 2000;
export const AUDIT_CROP = "hotspot-crop.png";

/** Image URLs the API serves (origin-relative, always under /anomalyguessr/api). */
export function imageUrl(
  kind: "image" | "original" | "audit",
  id: string,
): string {
  return `/anomalyguessr/api/scenes/${id}/${kind}`;
}
