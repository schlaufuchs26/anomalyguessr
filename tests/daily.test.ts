import { describe, expect, test } from "bun:test";
import {
  DAILY_COUNT,
  dateKey,
  dateLabel,
  hashString,
  mulberry32,
  pickDaily,
} from "../daily";

function scene(id: string) {
  return { id };
}

const POOL = Array.from({ length: 9 }, (_, i) => scene(`s${i + 1}`));

describe("dateKey / dateLabel", () => {
  test("formats a date as YYYY-MM-DD with zero padding", () => {
    expect(dateKey(new Date(2026, 8, 7))).toBe("2026-09-07");
    expect(dateKey(new Date(2026, 11, 31))).toBe("2026-12-31");
  });

  test("formats a German display label", () => {
    expect(dateLabel(new Date(2026, 8, 7))).toBe("7. September 2026");
  });
});

describe("hashString / mulberry32", () => {
  test("hash is stable and differs for close dates", () => {
    expect(hashString("2026-09-07")).toBe(hashString("2026-09-07"));
    expect(hashString("2026-09-07")).not.toBe(hashString("2026-09-08"));
  });

  test("mulberry32 stays in [0,1)", () => {
    const rng = mulberry32(42);
    for (let i = 0; i < 100; i++) {
      const v = rng();
      expect(v).toBeGreaterThanOrEqual(0);
      expect(v).toBeLessThan(1);
    }
    // deterministic
    expect(mulberry32(42)()).toBe(mulberry32(42)());
  });
});

describe("pickDaily", () => {
  test("returns DAILY_COUNT scenes from the pool", () => {
    const picked = pickDaily(POOL, new Date(2026, 8, 7));
    expect(picked).toHaveLength(DAILY_COUNT);
    for (const s of picked) {
      expect(POOL.some((p) => p.id === s.id)).toBe(true);
    }
  });

  test("same date gives the identical set and order (determinism)", () => {
    const a = pickDaily(POOL, new Date(2026, 8, 7));
    const b = pickDaily(POOL, new Date(2026, 8, 7));
    expect(a.map((s) => s.id)).toEqual(b.map((s) => s.id));
  });

  test("different dates rotate the set", () => {
    const sets = new Set<string>();
    for (let day = 1; day <= 10; day++) {
      const picked = pickDaily(POOL, new Date(2026, 8, day));
      sets.add(picked.map((s) => s.id).join(","));
    }
    // 10 consecutive days should not all collapse onto one set
    expect(sets.size).toBeGreaterThanOrEqual(8);
  });

  test("does not repeat scenes within one day", () => {
    const picked = pickDaily(POOL, new Date(2026, 8, 7));
    expect(new Set(picked.map((s) => s.id)).size).toBe(picked.length);
  });

  test("handles a pool smaller than the count", () => {
    const small = [scene("a"), scene("b"), scene("c")];
    expect(pickDaily(small, new Date(2026, 8, 7))).toHaveLength(3);
  });

  test("does not mutate the input pool", () => {
    const before = POOL.map((s) => s.id);
    pickDaily(POOL, new Date(2026, 8, 7));
    expect(POOL.map((s) => s.id)).toEqual(before);
  });
});
