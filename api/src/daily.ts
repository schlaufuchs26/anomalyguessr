import {
  type FeedbackFile,
  SCENE_ID_RE,
  type SceneEntry,
  type StateFile,
} from "./types.ts";

/**
 * The daily pick rule (tickets #1208 + #1229 + #1232), the TypeScript copy of
 * the origin in pipeline/ag_queue.py (day_candidates / choose_day /
 * daily_order). The Go copy in core/platforms/api/dashboard_temporal_daily.go
 * (tdDailyOrder / tdPickDay) was deleted in #1242.
 *
 * The fixture cases live in pipeline/test_ag_queue.py (RecycleTest #1206,
 * AnomalyVarietyTest #1229, FamilyVarietyTest #1232); the cases in
 * tests/daily.test.ts mirror those. If the Python pick changes, this file and
 * those tests must change together.
 */

/** Size of one shipped daily set; mirrors ag_queue.py's DAILY_COUNT. */
export const DAILY_COUNT = 5;

/**
 * The moderation tags that steer the day (tickets #1502/#1541, rule #1542):
 * scene ids Evan marked "funny" or "great" in feedback.json.
 */
export interface DayTags {
  funny?: ReadonlySet<string>;
  great?: ReadonlySet<string>;
}

/** A scene's shown date, "" when never shown (mirrors ag_queue.py's truthiness). */
function shownValue(e: SceneEntry): string {
  return e.shown ?? "";
}

/** `added` then `id`, oldest first (ag_queue.py's unshown_oldest_first key). */
function byAddedThenID(a: SceneEntry, b: SceneEntry): number {
  return a.added.localeCompare(b.added) || a.id.localeCompare(b.id);
}

/** `shown`, `added` then `id`, least recently shown first (shown_oldest_first key). */
function byShownThenAddedThenID(a: SceneEntry, b: SceneEntry): number {
  return (
    shownValue(a).localeCompare(shownValue(b)) ||
    a.added.localeCompare(b.added) ||
    a.id.localeCompare(b.id)
  );
}

/**
 * Ship-eligible scenes in the #1206 freshness order: never-shown accepted
 * scenes oldest-added first, then the back catalogue least-recently-shown
 * first. Rejected scenes never ship and unmoderated ones are playtest-only,
 * so only accepted-minus-rejected entries appear (ag_queue.py's
 * day_candidates = unshown_oldest_first + shown_oldest_first).
 */
export function shipCandidates(st: StateFile, fb: FeedbackFile): SceneEntry[] {
  const fresh: SceneEntry[] = [];
  const back: SceneEntry[] = [];
  for (const e of Object.values(st.scenes)) {
    if (!SCENE_ID_RE.test(e.id)) continue; // defensive: never surface unsafe ids
    if (e.id in fb.rejected) continue; // rejected: never shown or shipped again
    if (!(e.id in fb.accepted)) continue; // unmoderated: playtest-only
    if (shownValue(e) === "") fresh.push(e);
    else back.push(e);
  }
  fresh.sort(byAddedThenID);
  back.sort(byShownThenAddedThenID);
  return [...fresh, ...back];
}

/**
 * Pick up to `limit` scenes for one day, preferring variety (ag_queue.py's
 * choose_day). Candidate order is the freshness order; three passes:
 *
 *  1. take a candidate only when neither its anomaly label nor its family is
 *     already in the day;
 *  2. fill from the skipped candidates whose label is new;
 *  3. fill any remaining slots from whatever is left.
 *
 * Order within each pass is preserved; never the same scene id twice. A pool
 * with fewer than `limit` distinct labels or families still fills the day
 * with repeats.
 *
 * `tags` are the tagged scene ids from feedback.json (#1502/#1541): after the
 * variety passes the day also carries one "funny" and one "great" scene when
 * the pool has one (ag_queue.py's tag rule, ticket #1542).
 */
export function pickDay(
  candidates: SceneEntry[],
  limit: number,
  tags: DayTags = {},
): SceneEntry[] {
  const day: SceneEntry[] = [];
  const taken = new Set<string>();
  const labels = new Set<string>();
  const families = new Set<string>();
  const skipped: SceneEntry[] = [];
  for (const e of candidates) {
    if (day.length >= limit) break;
    if (taken.has(e.id)) continue;
    if (
      (e.anomaly !== "" && labels.has(e.anomaly)) ||
      (e.family && families.has(e.family))
    ) {
      skipped.push(e);
      continue;
    }
    day.push(e);
    taken.add(e.id);
    if (e.anomaly !== "") labels.add(e.anomaly);
    if (e.family) families.add(e.family);
  }
  if (day.length < limit) {
    for (const e of skipped) {
      if (day.length >= limit) break;
      if (taken.has(e.id)) continue;
      if (e.anomaly !== "" && labels.has(e.anomaly)) continue;
      day.push(e);
      taken.add(e.id);
      if (e.anomaly !== "") labels.add(e.anomaly);
    }
  }
  if (day.length < limit) {
    for (const e of skipped) {
      if (day.length >= limit) break;
      if (taken.has(e.id)) continue;
      day.push(e);
      taken.add(e.id);
    }
  }
  return placeTags(
    day,
    candidates,
    tags.funny ?? new Set(),
    tags.great ?? new Set(),
  );
}

/**
 * Swap a funny and a great scene into the day when the pool has one
 * (ag_queue.py's `_place_tags`, ticket #1542). Funny first, then great, each
 * pass protecting the other tag, so a swap for one never evicts the scene the
 * other just placed. A tag the day already carries is left alone; a tag no
 * untagged pick can make room for leaves the day as it is.
 */
function placeTags(
  day: SceneEntry[],
  candidates: SceneEntry[],
  funny: ReadonlySet<string>,
  great: ReadonlySet<string>,
): SceneEntry[] {
  let out = day;
  for (const [tagIds, protect] of [
    [funny, great],
    [great, funny],
  ] as const) {
    if (tagIds.size === 0) continue;
    const ids = new Set(out.map((e) => e.id));
    if (out.some((e) => tagIds.has(e.id))) continue;
    const incoming = candidates.find((e) => tagIds.has(e.id) && !ids.has(e.id));
    if (incoming === undefined) continue;
    const evictable = out
      .map((_e, i) => i)
      .filter(
        (i) => !tagIds.has(out[i]?.id ?? "") && !protect.has(out[i]?.id ?? ""),
      );
    if (evictable.length === 0) continue;
    out = swapIn(out, evictable, incoming);
  }
  return out;
}

/**
 * Replace the last evictable pick with `incoming`, in place (ag_queue.py's
 * `_swap_in`). A pick whose label already repeats in the day goes first:
 * dropping one of two "Bottle" scenes costs no label variety, dropping the
 * only "Robot" does.
 */
function swapIn(
  day: SceneEntry[],
  evictable: number[],
  incoming: SceneEntry,
): SceneEntry[] {
  const labels = day.map((e) => e.anomaly);
  const repeats = evictable.filter(
    (i) => labels[i] !== "" && labels.filter((l) => l === labels[i]).length > 1,
  );
  const order = repeats.length > 0 ? repeats : evictable;
  const idx = order.at(-1);
  if (idx === undefined) return day;
  return day.map((e, i) => (i === idx ? incoming : e));
}

/**
 * Ship-eligible scene ids in the exact order ag_queue.py's ship() would pick
 * them (ag_queue.py's daily_order): the day's set first (pickDay,
 * variety-preferred, including the #1542 tag rule), then the remaining
 * candidates in freshness order. This is what the gallery reads as
 * `dailyOrder`, so the UI never re-derives the order.
 */
export function dailyOrder(st: StateFile, fb: FeedbackFile): string[] {
  const candidates = shipCandidates(st, fb);
  const day = pickDay(candidates, DAILY_COUNT, {
    funny: new Set(Object.keys(fb.funny ?? {})),
    great: new Set(Object.keys(fb.great ?? {})),
  });
  const chosen = new Set(day.map((e) => e.id));
  return [
    ...day.map((e) => e.id),
    ...candidates.filter((e) => !chosen.has(e.id)).map((e) => e.id),
  ];
}
