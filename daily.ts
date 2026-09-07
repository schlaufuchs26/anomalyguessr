/**
 * Daily-quiz helpers. Version-2 pipeline manifests ship the day's 5 scenes
 * ready-made (the frontend plays them in manifest order); pickDaily is the
 * legacy fallback for version-1 pool manifests, where the frontend derives
 * the day's set deterministically from the local date (TimeGuessr model).
 */

export const DAILY_COUNT = 5;

const MONTHS_DE = [
  "Januar",
  "Februar",
  "März",
  "April",
  "Mai",
  "Juni",
  "Juli",
  "August",
  "September",
  "Oktober",
  "November",
  "Dezember",
];

/** Local calendar date as YYYY-MM-DD (the quiz day key). */
export function dateKey(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/** German display label, e.g. "7. September 2026". */
export function dateLabel(date: Date): string {
  return `${date.getDate()}. ${MONTHS_DE[date.getMonth()] ?? ""} ${date.getFullYear()}`;
}

/** Parses a YYYY-MM-DD manifest date into a local Date (no TZ surprises). */
export function dateFromKey(key: string): Date {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(key);
  if (!m) throw new Error(`invalid date key: ${key}`);
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

/** FNV-1a-ish string hash → 32-bit unsigned seed. */
export function hashString(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** Small deterministic PRNG (mulberry32) for a given seed. */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Deterministically pick `count` scenes for a date: seeded shuffle of the
 * pool, take the first `count`. Same date → same set and order for everyone.
 */
export function pickDaily<T extends { id: string }>(
  scenes: T[],
  date: Date,
  count = DAILY_COUNT,
): T[] {
  const rng = mulberry32(hashString(dateKey(date)));
  const pool = [...scenes];
  for (let i = pool.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    const tmp = pool[i];
    pool[i] = pool[j] as T;
    pool[j] = tmp as T;
  }
  return pool.slice(0, Math.min(count, pool.length));
}
