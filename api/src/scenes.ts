import path from "node:path";

import { DAILY_COUNT, dailyOrder } from "./daily.ts";
import { unshownAccepted } from "./generate.ts";
import {
  type Comment,
  type FeedbackFile,
  imageUrl,
  type Moderation,
  type SceneEntry,
  type StateFile,
} from "./types.ts";

/** API representation of one queue scene (matches the old TDScene shape). */
export interface ApiScene {
  id: string;
  title: string;
  place: string;
  year: string;
  credit?: string;
  sourceUrl?: string;
  source?: Record<string, unknown>;
  anomaly: string;
  description: string;
  answer: { x: number; y: number; r: number };
  hints: string[];
  added: string;
  shown: string | null;
  state: "unshown" | "shown" | "rejected";
  rejected: boolean;
  rejectedAt?: string;
  moderation: Moderation;
  comments: Comment[];
  /** A generation trace sidecar exists (ticket #1373): the gallery offers
   *  the "Generation trace" panel only when this is true. The trace body
   *  itself is fetched on open from /traces/{id}, never inlined here. */
  hasTrace: boolean;
  images: { edited: string; original: string; audit?: string };
}

export interface SceneList {
  summary: {
    total: number;
    unshown: number;
    shown: number;
    accepted: number;
    rejected: number;
    unmoderated: number;
    /** Subset of unshown a human accepted: the next daily's fresh pool. */
    unshownAccepted: number;
  };
  scenes: ApiScene[];
  /** Ship-eligible ids in the pipeline's pick order; the first dailyCount are next up. */
  dailyOrder: string[];
  dailyCount: number;
}

/** A manifest scene as the game's manifest.ts parses it (v2 shape). */
export interface ManifestScene {
  id: string;
  title: string;
  place: string;
  year: string;
  credit: string;
  sourceUrl: string;
  source: Record<string, unknown>;
  anomaly: string;
  explanation?: string;
  references?: { label: string; url: string }[];
  description: string;
  answer: { x: number; y: number; r: number };
  hints: string[];
  image: string;
  original: string;
}

export interface Manifest {
  version: number;
  date: string;
  scope: string;
  moderation?: boolean;
  scenes: ManifestScene[];
}

const SCOPES = new Set([
  "all",
  "unshown",
  "shown",
  "accepted",
  "unmoderated",
  "daily",
  "live",
]);

export function isScope(v: unknown): v is string {
  return typeof v === "string" && SCOPES.has(v);
}

/**
 * Render every state.json scene as its API JSON (newest added first),
 * attaching curation state + image URLs. Never surfaces unsafe ids.
 * `hasAudit` and `hasTrace` are (id) => boolean probes supplied by the
 * caller so the list can avoid per-scene stat calls in the hot path when
 * needed.
 */
export function listScenes(
  st: StateFile,
  fb: FeedbackFile,
  hasAudit: (id: string) => boolean,
  hasTrace: (id: string) => boolean = () => false,
): SceneList {
  const scenes: ApiScene[] = [];
  for (const e of Object.values(st.scenes)) {
    if (!/^[a-z0-9][a-z0-9-]*$/.test(e.id)) continue;
    scenes.push(sceneToApi(e, fb, e.id, hasAudit, hasTrace));
  }
  scenes.sort(
    (a, b) => b.added.localeCompare(a.added) || a.id.localeCompare(b.id),
  );

  const summary = {
    total: scenes.length,
    unshown: 0,
    shown: 0,
    accepted: 0,
    rejected: 0,
    unmoderated: 0,
    unshownAccepted: unshownAccepted(st, fb),
  };
  for (const s of scenes) {
    if (s.state === "shown") summary.shown++;
    else if (s.state === "unshown") summary.unshown++;
    // rejected counts only toward the moderation `rejected` count below
    if (s.moderation === "accepted") summary.accepted++;
    else if (s.moderation === "rejected") summary.rejected++;
    else summary.unmoderated++;
  }
  return {
    summary,
    scenes,
    dailyOrder: dailyOrder(st, fb),
    dailyCount: DAILY_COUNT,
  };
}

/** Render one stored entry as API JSON, attaching curation + image URLs. */
export function sceneToApi(
  e: SceneEntry,
  fb: FeedbackFile,
  id: string,
  hasAudit: (id: string) => boolean,
  hasTrace: (id: string) => boolean = () => false,
): ApiScene {
  const rejected = id in fb.rejected;
  const accepted = id in fb.accepted;
  const rejectedAt = fb.rejected[id];
  const moderation: Moderation = rejected
    ? "rejected"
    : accepted
      ? "accepted"
      : "unmoderated";
  const images: ApiScene["images"] = {
    edited: imageUrl("image", id),
    original: imageUrl("original", id),
  };
  if (hasAudit(id)) images.audit = imageUrl("audit", id);
  const out: ApiScene = {
    id,
    title: e.title,
    place: e.place,
    year: e.year,
    credit: e.credit,
    sourceUrl: e.sourceUrl,
    anomaly: e.anomaly,
    description: e.description,
    answer: e.answer,
    hints: e.hints,
    added: e.added,
    shown: e.shown,
    state: rejected ? "rejected" : e.shown != null ? "shown" : "unshown",
    rejected,
    moderation,
    comments: fb.comments[id] ?? [],
    hasTrace: hasTrace(id),
    images,
  };
  if (e.source !== undefined) out.source = e.source;
  if (rejectedAt !== undefined) out.rejectedAt = rejectedAt;
  return out;
}

/** The audit crop path for a scene, for existence probes + serving. */
export function auditCropPath(dataDir: string, id: string): string {
  return path.join(dataDir, "audit", id, "hotspot-crop.png");
}

/** A scene's generation trace sidecar (ticket #1373), for probes + serving. */
export function tracePath(dataDir: string, id: string): string {
  return path.join(dataDir, "traces", `${id}.json`);
}

/** A scene's library image path (edited or original) under the data dir. */
export function libraryImagePath(
  dataDir: string,
  id: string,
  kind: "image" | "original",
): string {
  const suffix = kind === "original" ? "-original" : "";
  return path.join(dataDir, "library", id, `${id}${suffix}.jpg`);
}

/**
 * Render one queue entry as the manifest scene the game's manifest.ts parses.
 * Callers skip entries without the provenance block: version-2 manifests
 * require `source` and the game rejects the whole manifest when one scene is
 * malformed. `family` is queue-side variety metadata (#1232) and deliberately
 * never leaves the API in a manifest.
 */
export function renderManifestScene(e: SceneEntry): ManifestScene {
  return {
    id: e.id,
    title: e.title,
    place: e.place,
    year: e.year,
    credit: e.credit,
    sourceUrl: e.sourceUrl,
    source: e.source ?? {},
    anomaly: e.anomaly,
    ...(e.explanation !== undefined ? { explanation: e.explanation } : {}),
    ...(e.references !== undefined ? { references: e.references } : {}),
    description: e.description,
    answer: e.answer,
    hints: e.hints,
    image: imageUrl("image", e.id),
    original: imageUrl("original", e.id),
  };
}

/**
 * Render the day's daily set (ticket #1221): the first DAILY_COUNT renderable
 * scenes of dailyOrder, i.e. exactly the scenes ag_queue.py's ship() would
 * write into the game's manifest right now, in ship order. The dev frontend
 * plays this for its Daily mode. Sourceless legacy entries are skipped: a
 * version-2 manifest cannot carry them.
 */
export function manifestDaily(
  st: StateFile,
  fb: FeedbackFile,
): ManifestScene[] {
  const out: ManifestScene[] = [];
  for (const id of dailyOrder(st, fb)) {
    if (out.length >= DAILY_COUNT) break;
    const e = st.scenes[id];
    if (e === undefined || e.source == null) continue;
    out.push(renderManifestScene(e));
  }
  return out;
}

/**
 * Render the daily set currently live on prod (ticket #1237): the scenes
 * state.json marks as shown on last_shipped, i.e. exactly the set the last
 * ship() wrote into the game's manifest. The dev instance plays it for its
 * Live mode, a replay of what a player sees on anomalyguessr.com right now.
 *
 * Membership is shown == last_shipped; rejected scenes stay out like in every
 * other scope (a scene rejected after the ship is not served again), and an
 * entry without the provenance block cannot be a version-2 manifest scene.
 * Order is the recorded ship order (last_shipped_ids, written by
 * ag_queue.py ship()); days shipped before that field existed keep a
 * deterministic (added, id) fallback instead of the manifest's.
 */
export function manifestLive(st: StateFile, fb: FeedbackFile): ManifestScene[] {
  const live: SceneEntry[] = [];
  if (st.last_shipped) {
    for (const e of Object.values(st.scenes)) {
      if (!/^[a-z0-9][a-z0-9-]*$/.test(e.id)) continue;
      if ((e.shown ?? "") !== st.last_shipped) continue;
      if (e.id in fb.rejected) continue;
      live.push(e);
    }
  }
  live.sort(liveOrder(st.last_shipped_ids ?? []));
  const out: ManifestScene[] = [];
  for (const e of live) {
    if (e.source == null) continue;
    out.push(renderManifestScene(e));
  }
  return out;
}

/**
 * Order the live set by the recorded ship order, ascending; ids absent from
 * the record come last by (added, id) so the result stays deterministic.
 */
function liveOrder(
  recorded: string[],
): (a: SceneEntry, b: SceneEntry) => number {
  const rank = new Map<string, number>();
  for (const [i, id] of recorded.entries()) {
    if (!rank.has(id)) rank.set(id, i); // first occurrence wins
  }
  return (a, b) => {
    const ra = rank.get(a.id);
    const rb = rank.get(b.id);
    if ((ra !== undefined) !== (rb !== undefined))
      return ra !== undefined ? -1 : 1;
    if (ra !== undefined && rb !== undefined && ra !== rb) return ra - rb;
    return a.added.localeCompare(b.added) || a.id.localeCompare(b.id);
  };
}

/**
 * The live manifest's date: the ship date, so the game's end screen names
 * the day the set went live, or today when no ship is recorded yet (the
 * live set is empty then anyway).
 */
export function liveDate(st: StateFile): string {
  return st.last_shipped || localDate();
}

/** Today as YYYY-MM-DD in server-local time (Go's time.Now().Format("2006-01-02")). */
export function localDate(d: Date = new Date()): string {
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/**
 * Render the non-rejected queue as a playable v2 manifest. scope selects
 * all / unshown / shown / accepted / unmoderated; daily delegates to
 * manifestDaily (the day's set) and live to manifestLive (the set prod is
 * serving now). Scenes without the provenance block (`source`) are skipped:
 * the game's manifest.ts rejects the whole manifest when a v2 scene is
 * malformed. Rejected scenes are always omitted. Order: never-shipped
 * scenes first, then shipped / accepted / unmoderated per scope selection,
 * newest added first within each group (mirrors the Go handler).
 */
export function manifestScenes(
  st: StateFile,
  fb: FeedbackFile,
  scope: string,
): ManifestScene[] {
  if (scope === "daily") return manifestDaily(st, fb);
  if (scope === "live") return manifestLive(st, fb);

  const groups: Record<
    "unshown" | "shown" | "accepted" | "unmoderated",
    SceneEntry[]
  > = {
    unshown: [],
    shown: [],
    accepted: [],
    unmoderated: [],
  };
  for (const e of Object.values(st.scenes)) {
    if (!/^[a-z0-9][a-z0-9-]*$/.test(e.id)) continue;
    if (e.id in fb.rejected) continue;
    if (e.source == null) continue; // legacy entry: cannot be a v2 scene
    const accepted = e.id in fb.accepted;
    if (scope === "accepted" && accepted) groups.accepted.push(e);
    else if (scope === "unmoderated" && !accepted) groups.unmoderated.push(e);
    else if (e.shown == null && (scope === "all" || scope === "unshown"))
      groups.unshown.push(e);
    else if (e.shown != null && (scope === "all" || scope === "shown"))
      groups.shown.push(e);
  }
  const newestFirst = (a: SceneEntry, b: SceneEntry): number =>
    b.added.localeCompare(a.added) || a.id.localeCompare(b.id);

  const out: ManifestScene[] = [];
  for (const key of scope === "accepted"
    ? (["accepted"] as const)
    : scope === "unmoderated"
      ? (["unmoderated"] as const)
      : (["unshown", "shown"] as const)) {
    for (const e of groups[key].sort(newestFirst))
      out.push(renderManifestScene(e));
  }
  return out;
}
