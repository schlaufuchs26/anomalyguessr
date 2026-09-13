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

/** The last checker verdict the generator stored with a scene (#1436). */
export interface SceneChecker {
  /** Requirements met, 0..7 (computed from the failed numbers). */
  score: number;
  /** Numbers (1..7) of the requirements the shipped image failed. */
  failed: number[];
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

export interface FeedbackFile {
  version: number;
  /** Scene id -> RFC3339 timestamp (written by this API). */
  rejected: Record<string, string>;
  /** Pre-#1207 legacy name of `rejected`; normalizeFeedback folds it in. */
  excluded?: Record<string, string>;
  accepted: Record<string, string>;
  comments: Record<string, Comment[]>;
}

export type Moderation = "accepted" | "rejected" | "unmoderated";

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
