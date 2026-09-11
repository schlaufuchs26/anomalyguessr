/**
 * Component tests for the React port of the game (ticket #1172). They replace
 * the old DOM-level tests that drove the vanilla game.ts singleton: the app is
 * rendered with @testing-library/react, the manifest fetch is stubbed, and the
 * flows run through the rendered UI (guess -> reveal -> compare -> next -> end
 * screen, hints, moderation).
 *
 * Layout: happy-dom has no layout engine, so the photo's box is stubbed via
 * getBoundingClientRect; without it the click-to-guess math has no frame to map
 * into (the same reason the old test called game.resolve() directly).
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { App } from "../frontend";
import { makeScene } from "./fixtures";

const SCENE_A = makeScene({
  id: "a",
  title: "Scene A",
  x: 0.5,
  y: 0.5,
  r: 0.05,
});
const SCENE_B = makeScene({
  id: "b",
  title: "Scene B",
  x: 0.2,
  y: 0.8,
  r: 0.04,
});
const MANIFEST = { version: 2, date: "2026-09-10", scenes: [SCENE_A, SCENE_B] };

/** Payload the stubbed manifest fetch answers with ("fail" -> HTTP 500). */
let payload: unknown = MANIFEST;
/** Requests the app made to the moderation endpoint. */
let posts: { url: string; body: unknown }[] = [];
/** Every URL the app fetched (documents the manifest/API base decision). */
let gets: string[] = [];
/** Whether the stubbed media query reports the laptop layout. */
let desktop = true;

/** The photo's box: 800x600 at the origin, in viewport coordinates. */
const RECT = {
  left: 0,
  top: 0,
  width: 800,
  height: 600,
  right: 800,
  bottom: 600,
  x: 0,
  y: 0,
  toJSON: () => ({}),
};

beforeEach(() => {
  payload = MANIFEST;
  posts = [];
  gets = [];
  desktop = true;
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "POST") {
      posts.push({ url, body: JSON.parse(String(init.body)) });
      return new Response("{}", { status: 200 });
    }
    gets.push(url);
    if (payload === "fail") return new Response("boom", { status: 500 });
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as unknown as typeof fetch;
  HTMLElement.prototype.getBoundingClientRect = () => RECT as DOMRect;
  // happy-dom has no layout: give the stage a size and let the tests switch
  // between the laptop layout (stage-derived fit) and the stacked one.
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get: () => RECT.width,
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get: () => RECT.height,
  });
  window.matchMedia = ((query: string) => ({
    matches: desktop,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
});

/** Render the app and wait for the first scene to be on screen. */
async function renderGame(): Promise<HTMLElement> {
  const { container } = render(<App />);
  await screen.findByText("Scene A");
  // The scene mounts during the async manifest fetch, so React can still have
  // pending passive effects (the wheel/resize listeners in PhotoStage) when
  // findByText resolves. Flush them before the test dispatches events; without
  // this the wheel test flakes under load (ticket #1193).
  await act(async () => {});
  return container;
}

/** The photo overlay (re-query: it is remounted for every scene). */
function overlay(container: HTMLElement): HTMLElement {
  const el = container.querySelector<HTMLElement>("#overlay");
  if (!el) throw new Error("no overlay rendered");
  return el;
}

/** The original-photo layer (re-query for the same reason). */
function originalLayer(container: HTMLElement): HTMLImageElement {
  const el = container.querySelector<HTMLImageElement>("#photo-orig");
  if (!el) throw new Error("no original layer rendered");
  return el;
}

/**
 * Model a browser that finished loading the layer (`complete` + real
 * dimensions, as for a preloaded image) or one that is still downloading
 * (`complete === false`). happy-dom does not fetch file URLs, so the tests
 * state the two properties the reveal guard reads; a failed layer reports
 * `complete` with `naturalWidth === 0`.
 */
function setOriginalLoadState(
  orig: HTMLImageElement,
  state: "loaded" | "downloading" | "failed",
): HTMLImageElement {
  Object.defineProperty(orig, "complete", {
    value: state !== "downloading",
    configurable: true,
  });
  Object.defineProperty(orig, "naturalWidth", {
    value: state === "loaded" ? RECT.width : 0,
    configurable: true,
  });
  return orig;
}

/** Click the photo at normalized (nx, ny) through the real click handler. */
function clickPhoto(container: HTMLElement, nx: number, ny: number): void {
  fireEvent.click(overlay(container), {
    clientX: nx * RECT.width,
    clientY: ny * RECT.height,
  });
}

describe("scene rendering", () => {
  test("shows the scene meta, progress and the day's manifest", async () => {
    await renderGame();
    expect(screen.getByText("Scene A")).toBeInTheDocument();
    expect(screen.getByText("Dresden, 1900")).toBeInTheDocument();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    expect(screen.getByText("Context for Scene A.")).toBeInTheDocument();
    // manifest comes from the shipped scenes/ path (see src/api.ts, #1171 not wired)
    expect(gets).toEqual(["scenes/manifest.json"]);
  });

  test("shows the load error when the manifest cannot be loaded", async () => {
    payload = "fail";
    render(<App />);
    expect(
      await screen.findByText(/The scenes could not be loaded/),
    ).toBeInTheDocument();
  });
});

describe("hints", () => {
  test("reveal the three progressive hints and then disable", async () => {
    await renderGame();
    const hint = screen.getByRole("button", { name: "💡 Show hint" });

    fireEvent.click(hint);
    expect(
      screen.getByText("Hint 1/3: Attached to a person."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "💡 Show hint (1/3)" }),
    ).toBeInTheDocument();

    fireEvent.click(hint);
    fireEvent.click(hint);
    expect(
      screen.getByText("Hint 3/3: In the sky: a drone."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "💡 No hints left" }),
    ).toBeDisabled();
  });

  test("a hint lowers the score of a perfect hit (0.85 multiplier)", async () => {
    const container = await renderGame();
    fireEvent.click(screen.getByRole("button", { name: "💡 Show hint" }));
    clickPhoto(container, 0.5, 0.5);
    expect(screen.getByText(/85 points/)).toBeInTheDocument();
    expect(screen.getByText(/1 hint used/)).toBeInTheDocument();
  });
});

describe("guess, reveal and compare", () => {
  test("a hit whose original is already loaded reveals it immediately (#1185)", async () => {
    const container = await renderGame();
    setOriginalLoadState(originalLayer(container), "loaded");

    clickPhoto(container, 0.5, 0.5);
    expect(screen.getByText("Timeline secured!")).toBeInTheDocument();
    // The layer is preloaded, so no load event follows the guess: the reveal
    // has to run right away instead of waiting for one that never arrives.
    expect(originalLayer(container).classList.contains("reveal")).toBe(true);
    expect(
      container.querySelector("#photo")?.classList.contains("correcting"),
    ).toBe(true);
    expect(
      screen.getByText(/Now showing the original photo/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "🖼️ Edited image" }),
    ).toBeInTheDocument();
  });

  test("a broken original is reported instead of waiting forever", async () => {
    const container = await renderGame();
    // The layer has no dimensions: the source is broken (or gone), so no
    // load event will ever come and the reveal must not pretend to work.
    setOriginalLoadState(originalLayer(container), "failed");
    clickPhoto(container, 0.5, 0.5);

    expect(originalLayer(container).classList.contains("reveal")).toBe(false);
    expect(
      screen.getByText(/The original photo could not be loaded/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "📷 Show original" }),
    ).toBeInTheDocument();
  });

  test("an original that fails while the guess waits reports the failure", async () => {
    const container = await renderGame();
    setOriginalLoadState(originalLayer(container), "downloading");
    clickPhoto(container, 0.5, 0.5);
    expect(originalLayer(container).classList.contains("reveal")).toBe(false);

    // the download failed after the guess, so the wait has to end with a note
    fireEvent.error(originalLayer(container));
    expect(originalLayer(container).classList.contains("reveal")).toBe(false);
    expect(
      screen.getByText(/The original photo could not be loaded/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "📷 Show original" }),
    ).toBeInTheDocument();
  });

  test("a perfect hit secures the timeline and crossfades to the original", async () => {
    const container = await renderGame();
    setOriginalLoadState(originalLayer(container), "downloading");

    clickPhoto(container, 0.5, 0.5);
    expect(screen.getByText("Timeline secured!")).toBeInTheDocument();
    expect(screen.getByText(/100 points/)).toBeInTheDocument();
    expect(screen.getByText(/no hints/)).toBeInTheDocument();
    expect(
      screen.getByText(/Exact spot: In the sky: a drone\./),
    ).toBeInTheDocument();
    // the why-reveal carries the checkable reference link
    expect(
      screen.getByRole("link", { name: "History of drones" }),
    ).toHaveAttribute("href", "https://example.org/drones");

    // a hit asks for the original; the animation starts once it has loaded
    const orig = originalLayer(container);
    expect(
      screen.getByRole("button", { name: "🖼️ Edited image" }),
    ).toBeInTheDocument();
    expect(orig.classList.contains("reveal")).toBe(false);

    fireEvent.load(orig);
    expect(originalLayer(container).classList.contains("reveal")).toBe(true);
    expect(
      container.querySelector("#photo")?.classList.contains("correcting"),
    ).toBe(true);
    expect(
      screen.getByText(/Now showing the original photo/),
    ).toBeInTheDocument();
    // click marker + answer circle are placed
    expect(container.querySelectorAll(".marker.answer")).toHaveLength(1);
    expect(container.querySelectorAll(".marker.click")).toHaveLength(1);
  });

  test("a miss keeps the edited photo; compare toggles the original manually", async () => {
    const container = await renderGame();

    clickPhoto(container, 0.95, 0.95);
    expect(screen.getByText("Miss.")).toBeInTheDocument();
    const orig = originalLayer(container);
    expect(orig.classList.contains("reveal")).toBe(false);
    expect(orig.classList.contains("show")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "📷 Show original" }));
    expect(originalLayer(container).classList.contains("show")).toBe(true);
    expect(
      screen.getByRole("button", { name: "🖼️ Edited image" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "🖼️ Edited image" }));
    expect(originalLayer(container).classList.contains("show")).toBe(false);
    expect(
      screen.getByRole("button", { name: "📷 Show original" }),
    ).toBeInTheDocument();
  });

  test("a warm click (near miss) does not reveal the original", async () => {
    const container = await renderGame();
    clickPhoto(container, 0.7, 0.5); // distance 0.2: warm, outside the circle
    expect(screen.getByText("Close!")).toBeInTheDocument();
    const orig = originalLayer(container);
    expect(orig.classList.contains("reveal")).toBe(false);
    expect(orig.classList.contains("show")).toBe(false);
  });

  test("focus moves to next after a guess (keyboard play)", async () => {
    const container = await renderGame();
    clickPhoto(container, 0.5, 0.5);
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("button", { name: "Next →" }),
      ),
    );
  });
});

describe("run flow", () => {
  test("next moves to the following scene and the run ends with the score", async () => {
    const container = await renderGame();
    clickPhoto(container, 0.5, 0.5); // 100 points on scene A

    fireEvent.click(screen.getByRole("button", { name: "Next →" }));
    expect(screen.getByText("Scene B")).toBeInTheDocument();
    expect(screen.getByText("2 / 2")).toBeInTheDocument();

    clickPhoto(container, 0.95, 0.95); // 0 points on scene B
    fireEvent.click(screen.getByRole("button", { name: "Results →" }));

    expect(screen.getByText("Mission complete")).toBeInTheDocument();
    expect(
      screen.getByText("Daily quiz · September 10, 2026"),
    ).toBeInTheDocument();
    expect(screen.getByText("1. Scene A · 100 pts")).toBeInTheDocument();
    expect(screen.getByText("2. Scene B · 0 pts")).toBeInTheDocument();
    expect(
      screen.getByText(
        "100 of 200 points (avg 50) across 2 scenes. The timeline holds, for now.",
      ),
    ).toBeInTheDocument();
  });

  test("play again restarts the same day from the first scene", async () => {
    const container = await renderGame();
    clickPhoto(container, 0.5, 0.5);
    fireEvent.click(screen.getByRole("button", { name: "Next →" }));
    clickPhoto(container, 0.95, 0.95);
    fireEvent.click(screen.getByRole("button", { name: "Results →" }));

    fireEvent.click(screen.getByRole("button", { name: "Play again" }));
    expect(screen.getByText("Scene A")).toBeInTheDocument();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
  });
});

describe("photo interactions", () => {
  /** Simulate the base photo having loaded at the stubbed box size. */
  function readyPhoto(container: HTMLElement): void {
    const img = container.querySelector<HTMLImageElement>("#photo-img");
    if (!img) throw new Error("no base photo");
    Object.defineProperty(img, "naturalWidth", {
      value: RECT.width,
      configurable: true,
    });
    Object.defineProperty(img, "naturalHeight", {
      value: RECT.height,
      configurable: true,
    });
    fireEvent.load(img);
  }

  test("wheel zooms toward the cursor and never below 1x", async () => {
    const container = await renderGame();
    readyPhoto(container);
    const photo = container.querySelector<HTMLElement>("#photo");
    const ov = overlay(container);

    fireEvent.wheel(ov, { deltaY: -200, clientX: 400, clientY: 300 });
    const zoomed = photo?.style.transform ?? "";
    const scale = Number(/scale\(([\d.]+)\)/.exec(zoomed)?.[1]);
    expect(scale).toBeGreaterThan(1);

    // zooming back out clamps at identity instead of going below 1x
    for (let i = 0; i < 8; i++) {
      fireEvent.wheel(ov, { deltaY: 200, clientX: 400, clientY: 300 });
    }
    expect(photo?.style.transform).toContain("scale(1)");
  });

  test("double-click zooms in and again resets", async () => {
    const container = await renderGame();
    readyPhoto(container);
    const photo = container.querySelector<HTMLElement>("#photo");
    const ov = overlay(container);

    fireEvent.doubleClick(ov, { clientX: 400, clientY: 300 });
    expect(photo?.style.transform).toMatch(/scale\(3\)/);

    fireEvent.doubleClick(ov, { clientX: 400, clientY: 300 });
    expect(photo?.style.transform).toContain("scale(1)");
  });

  test("a drag pans instead of guessing", async () => {
    const container = await renderGame();
    readyPhoto(container);
    const ov = overlay(container);

    fireEvent.pointerDown(ov, { clientX: 100, clientY: 100, pointerId: 1 });
    fireEvent.pointerMove(ov, { clientX: 200, clientY: 220, pointerId: 1 });
    fireEvent.pointerUp(ov, { pointerId: 1 });
    // the gesture was a pan, so the click that follows is swallowed
    clickPhoto(container, 0.5, 0.5);
    expect(screen.queryByText("Timeline secured!")).not.toBeInTheDocument();
    expect(screen.queryByText("Miss.")).not.toBeInTheDocument();
  });

  test("double-click after a guess undoes it before zooming", async () => {
    const container = await renderGame();
    readyPhoto(container);
    clickPhoto(container, 0.5, 0.5);
    expect(screen.getByText("Timeline secured!")).toBeInTheDocument();

    fireEvent.doubleClick(overlay(container), { clientX: 400, clientY: 300 });
    expect(screen.queryByText("Timeline secured!")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "💡 Show hint" }),
    ).toBeInTheDocument();
    expect(container.querySelector("#photo")?.style.transform).toMatch(
      /scale\(3\)/,
    );
  });

  test("a viewport resize refits the photo", async () => {
    const container = await renderGame();
    readyPhoto(container);
    // zoom first: the refit has to reset it before re-measuring
    fireEvent.wheel(overlay(container), {
      deltaY: -200,
      clientX: 400,
      clientY: 300,
    });
    expect(container.querySelector("#photo")?.style.transform).toMatch(
      /scale\((?!1\))/,
    );
    fireEvent(window, new Event("resize"));
    expect(container.querySelector("#photo")?.style.transform).toContain(
      "scale(1)",
    );
  });

  test("on the stacked layout the photo keeps its CSS size", async () => {
    desktop = false;
    const container = await renderGame();
    readyPhoto(container);
    const img = container.querySelector<HTMLImageElement>("#photo-img");
    // no explicit pixel sizing outside the laptop layout (CSS max-width rules)
    expect(img?.style.width).toBe("");
  });
});

describe("moderation mode (dev instance, #1163)", () => {
  test("accept posts the verdict and feedback, then goes inert", async () => {
    payload = { ...MANIFEST, moderation: true };
    const container = await renderGame();
    clickPhoto(container, 0.5, 0.5);

    const feedback = screen.getByPlaceholderText(/Feedback for the pipeline/);
    fireEvent.change(feedback, { target: { value: "  nice scene  " } });
    fireEvent.click(screen.getByRole("button", { name: "✓ Accept" }));

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]?.url).toBe("/api/v1/anomalyguessr/scenes/a/moderate");
    expect(posts[0]?.body).toEqual({
      action: "accept",
      feedback: "nice scene",
    });
    expect(await screen.findByText("✓ Accepted.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "✓ Accept" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "✕ Reject" })).toBeDisabled();
  });

  test("a failed post surfaces the error and keeps the buttons usable", async () => {
    payload = { ...MANIFEST, moderation: true };
    const container = await renderGame();
    clickPhoto(container, 0.5, 0.5);
    globalThis.fetch = (async () =>
      new Response("nope", { status: 500 })) as unknown as typeof fetch;

    fireEvent.click(screen.getByRole("button", { name: "✕ Reject" }));
    expect(await screen.findByText(/Save failed/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "✕ Reject" })).not.toBeDisabled();
  });

  test("the box stays hidden outside moderation mode", async () => {
    const container = await renderGame();
    clickPhoto(container, 0.5, 0.5);
    expect(screen.queryByText("Moderation verdict")).not.toBeInTheDocument();
  });
});

afterEach(() => {
  globalThis.fetch = fetch;
});
