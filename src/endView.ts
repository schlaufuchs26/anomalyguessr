import type { Scene } from "../manifest";

/**
 * Pure end-screen copy and list data. Kept out of the React file so the
 * caller-relevant text stays unit-testable without rendering.
 */
export interface EndData {
  dateLabel: string;
  rows: { index: number; title: string; score: number }[];
  total: number;
  maxTotal: number;
  avg: number;
  text: string;
}

export function buildEndData(
  scenes: Scene[],
  scores: number[],
  dateLabel: string,
): EndData {
  const total = scores.reduce((sum, v) => sum + v, 0);
  const n = scenes.length;
  const avg = n > 0 ? Math.round(total / n) : 0;
  const rows = scenes.map((s, i) => ({
    index: i + 1,
    title: s.title,
    score: scores[i] ?? 0,
  }));
  return {
    dateLabel,
    rows,
    total,
    maxTotal: n * 100,
    avg,
    text: `${total} of ${n * 100} points (avg ${avg}) across ${n} scenes. The timeline holds, for now.`,
  };
}
