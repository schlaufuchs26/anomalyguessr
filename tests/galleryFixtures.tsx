import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { agUrl } from "../src/gallery/api";
import { GalleryPage } from "../src/gallery/GalleryPage";

// Shared fixtures for the AnomalyGuessr gallery tests (kept out of the
// *.test.tsx files so each stays under the 500-line gate).

export interface FixtureScene {
  id: string;
  shortId?: string;
  title: string;
  place: string;
  year: string;
  anomaly: string;
  added: string;
  shown: string | null;
  moderation: "accepted" | "rejected" | "unmoderated";
  comments: { text: string; createdAt: string }[];
  audit?: string;
  /** Answer circle override; null (or absent) means "no answer data" (#1209). */
  answer?: { x: number; y: number; r: number } | null;
  /** Rejection time override (ticket #1475); null models a legacy rejection
   *  recorded before the timestamp was written. Defaults to a fixed time on
   *  rejected scenes. */
  rejectedAt?: string | null;
  /** A generation trace sidecar exists (ticket #1373). */
  hasTrace?: boolean;
  /** Last checker verdict (ticket #1436). */
  checker?: { score: number; failed: number[]; reason: string };
  /** Unrepaired checker finding (ticket #1449). */
  needsReview?: boolean;
}

export function makeScene(over: Partial<FixtureScene>): {
  id: string;
  shortId?: string;
  title: string;
  place: string;
  year: string;
  credit: string;
  sourceUrl: string;
  source: Record<string, string>;
  anomaly: string;
  description: string;
  answer: { x: number; y: number; r: number } | null;
  hints: string[];
  added: string;
  shown: string | null;
  state: "unshown" | "shown" | "rejected";
  rejected: boolean;
  rejectedAt: string | null;
  moderation: "accepted" | "rejected" | "unmoderated";
  comments: { text: string; createdAt: string }[];
  hasTrace: boolean;
  images: { edited: string; original: string; audit?: string };
  checker?: { score: number; failed: number[]; reason: string };
  needsReview?: boolean;
} {
  const moderation = over.moderation ?? "unmoderated";
  const rejected = moderation === "rejected";
  const shown = over.shown ?? null;
  return {
    id: over.id ?? "scene",
    ...(over.shortId ? { shortId: over.shortId } : {}),
    title: over.title ?? "Scene",
    place: over.place ?? "Place",
    year: over.year ?? "1900",
    credit: "Public Domain",
    sourceUrl: "https://example.test/source",
    source: { repository: "Example", license: "PD" },
    anomaly: over.anomaly ?? "Objekt",
    description: "A busy historical market scene.",
    answer:
      over.answer === undefined ? { x: 0.4, y: 0.6, r: 0.05 } : over.answer,
    hints: ["hint 1", "hint 2", "hint 3"],
    added: over.added ?? "2026-09-08",
    shown,
    state: rejected ? "rejected" : shown ? "shown" : "unshown",
    rejected,
    rejectedAt:
      over.rejectedAt !== undefined
        ? over.rejectedAt
        : rejected
          ? "2026-09-08T08:00:00+02:00"
          : null,
    moderation,
    comments: over.comments ?? [],
    hasTrace: over.hasTrace ?? false,
    ...(over.checker ? { checker: over.checker } : {}),
    ...(over.needsReview ? { needsReview: true } : {}),
    images: {
      edited: agUrl(`scenes/${over.id}/image`),
      original: agUrl(`scenes/${over.id}/original`),
      ...(over.audit ? { audit: agUrl(`scenes/${over.id}/audit`) } : {}),
    },
  };
}

export type Scene = ReturnType<typeof makeScene>;

// Accepted-first fixture (ticket #1208): the daily view walks the API's
// dailyOrder, so f1/f2 (fresh) lead s1 (back catalogue); r1 and u1 are only
// reachable through their chips.
const DAILY_ORDER = ["f1", "f2", "s1"];

export const FIXTURE = [
  makeScene({
    id: "f1",
    shortId: "AG-3",
    title: "Fresh Market",
    place: "Freshtown",
    year: "1901",
    anomaly: "Digital watch",
    added: "2026-09-01",
    moderation: "accepted",
  }),
  makeScene({
    id: "f2",
    title: "Fresh Docks",
    place: "Harbor",
    year: "1902",
    anomaly: "Plastic cup",
    added: "2026-09-02",
    moderation: "accepted",
  }),
  makeScene({
    id: "s1",
    shortId: "AG-1",
    title: "Old Market",
    place: "Alpha",
    year: "1900",
    anomaly: "Plastic bottle",
    added: "2026-08-01",
    shown: "2026-08-10",
    moderation: "accepted",
    audit: "yes",
    hasTrace: true,
    comments: [
      { text: "well blended", createdAt: "2026-09-08T08:00:00+02:00" },
    ],
  }),
  makeScene({
    id: "r1",
    title: "Rejected Street",
    place: "Gamma",
    year: "1880",
    anomaly: "Smartphone",
    added: "2026-09-05",
    moderation: "rejected",
  }),
  makeScene({
    id: "u1",
    title: "Unmoderated Lane",
    place: "Delta",
    year: "1910",
    anomaly: "E-Scooter",
    added: "2026-09-06",
    moderation: "unmoderated",
  }),
];

export function listBody(
  scenes: Scene[],
  dailyOrder: string[] = DAILY_ORDER,
): {
  summary: {
    total: number;
    unshown: number;
    shown: number;
    accepted: number;
    rejected: number;
    unmoderated: number;
  };
  scenes: Scene[];
  dailyOrder: string[];
  dailyCount: number;
} {
  return {
    summary: {
      total: scenes.length,
      unshown: scenes.filter((s) => s.state === "unshown").length,
      shown: scenes.filter((s) => s.state === "shown").length,
      accepted: scenes.filter((s) => s.moderation === "accepted").length,
      rejected: scenes.filter((s) => s.moderation === "rejected").length,
      unmoderated: scenes.filter((s) => s.moderation === "unmoderated").length,
    },
    scenes,
    dailyOrder,
    dailyCount: 5,
  };
}

export function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <GalleryPage />
    </QueryClientProvider>,
  );
}

/** A generation trace sidecar fixture (ticket #1373), matching the API. */
export const TRACE = {
  scene: "s1",
  source: "commons-market-abc123",
  date: "2026-08-01",
  model: "deepseek/deepseek-v4.1-flash",
  image_model: "google/gemini-3.1-flash-image",
  recordedAt: "2026-08-01T03:20:11+02:00",
  calls: [
    {
      stage: "proposal",
      attempt: 1,
      model: "deepseek/deepseek-v4.1-flash",
      prompt: "Invent ONE subtle anomaly for this photograph.",
      answer: '{"anomaly": "Plastic bottle", "kind": "later-era"}',
      reasoning:
        "The market predates PET bottles, so a clear PET bottle reads as later-era.",
      image: "market.jpg",
      at: "2026-08-01T03:19:40+02:00",
      usage: {
        prompt_tokens: 980,
        completion_tokens: 130,
        reasoning_tokens: 42,
        cost: 0.0004,
      },
      duration_s: 3.4,
    },
    {
      stage: "edit",
      attempt: 1,
      model: "google/gemini-3.1-flash-image",
      prompt: "Edit this historical photograph: add ONE plastic bottle...",
      image: "market.jpg",
      at: "2026-08-01T03:19:44+02:00",
      duration_s: 9.1,
    },
    {
      stage: "coordinates",
      attempt: 1,
      model: "deepseek/deepseek-v4.1-flash",
      prompt: "Return the click target that covers the WHOLE added element.",
      answer: '{"x": 0.31, "y": 0.72, "r": 0.05}',
      image: "market-a1.png",
      at: "2026-08-01T03:19:53+02:00",
      usage: { prompt_tokens: 1200, completion_tokens: 40, cost: 0.0002 },
      duration_s: 2.8,
    },
  ],
};

/** A trace of the score guard's case (ticket #1465): a correction chain in
 *  which round 0 scored best, so the pipeline shipped it and kept round 2's
 *  render (checker 4) only in the trace. */
export const TRACE_GUARDED = {
  scene: "s1",
  source: "commons-market-abc123",
  date: "2026-09-13",
  model: "deepseek/deepseek-v4.1-flash",
  image_model: "google/gemini-2.5-flash-image",
  recordedAt: "2026-09-13T19:50:47+02:00",
  score_guard: { shipped: 0, score: 6, rejected: { round: 2, score: 4 } },
  calls: [
    { stage: "proposal", attempt: 1, model: "deepseek/deepseek-v4.1-flash" },
    { stage: "edit r0", attempt: 1, draw: 1, image: "market.jpg" },
    {
      stage: "check r0",
      attempt: 1,
      score: 6,
      failed: [2, 7],
      reason: "the wristband sits on the wrong wrist",
      image: "market-a1-r0-d1.png",
    },
    { stage: "fix-edit r1", attempt: 1, image: "market-a1-r0-d1.png" },
    {
      stage: "check r1",
      attempt: 1,
      score: 5,
      failed: [2, 5, 7],
      image: "market-a1-r1-fix.png",
    },
    { stage: "fix-edit r2", attempt: 1, image: "market-a1-r1-fix.png" },
    {
      stage: "check r2",
      attempt: 1,
      score: 4,
      failed: [2, 3, 5, 6],
      image: "market-a1-r2-fix.png",
    },
    { stage: "coordinates", attempt: 1, image: "market-a1-r0-d1.png" },
  ],
};

/** Serve listBody(scenes, dailyOrder) for GET /scenes and each scene's trace
 *  for GET /traces/{id} (404 otherwise). */
export function mockList(scenes: Scene[] = FIXTURE, dailyOrder?: string[]) {
  global.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === agUrl("scenes")) {
      return new Response(JSON.stringify(listBody(scenes, dailyOrder)), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    for (const s of scenes) {
      if (s.hasTrace && url === agUrl(`traces/${s.id}`)) {
        return new Response(JSON.stringify({ ...TRACE, scene: s.id }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
    }
    return new Response("not found", { status: 404 });
  }) as unknown as typeof fetch;
}
