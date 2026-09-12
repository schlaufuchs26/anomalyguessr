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

/** One queue scene as stored in state.json (a manifest scene minus image paths, plus added/shown). */
export interface SceneEntry {
  id: string;
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
  hints: string[];
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
export const MAX_COMMENT = 2000;
export const AUDIT_CROP = "hotspot-crop.png";

/** Image URLs the API serves (origin-relative, always under /anomalyguessr/api). */
export function imageUrl(
  kind: "image" | "original" | "audit",
  id: string,
): string {
  return `/anomalyguessr/api/scenes/${id}/${kind}`;
}
