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
/** Generator status the stubbed /generate GET answers with (#1210). */
let generateStatus: unknown = {
  state: "idle",
  running: false,
  buffer: 0,
  count: 0,
  planned: 0,
  added: 0,
  failed: 0,
  imageCalls: 0,
};
/** Non-null makes POST /generate fail (e.g. 409 already running, #1210). */
let startFailure: { status: number; detail: string } | null = null;
/** Payload a successful POST /generate answers with (default: generateStatus). */
let startResponse: unknown = null;
let startCalls = 0;
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
  // The path is the mode source of truth (#1223); reset it (and any 404-shim
  // stash) so every render starts on the frontpage again.
  history.replaceState(null, "", "/");
  sessionStorage.clear();
  payload = MANIFEST;
  posts = [];
  gets = [];
  desktop = true;
  generateStatus = {
    state: "idle",
    running: false,
    buffer: 0,
    count: 0,
    planned: 0,
    added: 0,
    failed: 0,
    imageCalls: 0,
  };
  startFailure = null;
  startResponse = null;
  startCalls = 0;
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/anomalyguessr/generate")) {
      if (init?.method === "POST") {
        startCalls++;
        if (startFailure) {
          return new Response(JSON.stringify({ title: startFailure.detail }), {
            status: startFailure.status,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify(startResponse ?? generateStatus), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify(generateStatus), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
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

/** Render the app and wait for the frontpage (manifest loaded, #1214). */
async function renderFrontpage(): Promise<HTMLElement> {
  const { container } = render(<App />);
  await screen.findByRole("button", { name: /^Daily/ });
  // The frontpage mounts during the async manifest fetch, so React can still
  // have pending passive effects when the query resolves. Flush them before
  // the test dispatches events; without this some tests flake under load.
  await act(async () => {});
  return container;
}

/** Pick a mode on the frontpage and wait for the first scene on screen. */
async function renderGame(
  mode: "daily" | "moderation" = "daily",
): Promise<HTMLElement> {
  const container = await renderFrontpage();
  fireEvent.click(
    screen.getByRole("button", {
      name: mode === "daily" ? /^Daily/ : /^Moderation/,
    }),
  );
  await screen.findByText("Scene A");
  await act(async () => {});
  return container;
}

/** Pick Moderation on the frontpage and wait for the empty queue state. */
async function renderModerationEmpty(): Promise<HTMLElement> {
  const container = await renderFrontpage();
  fireEvent.click(screen.getByRole("button", { name: /^Moderation/ }));
  await screen.findByText("Moderation queue is empty");
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

describe("frontpage mode select (#1214)", () => {
  test("prod offers only Daily and starts the day's set", async () => {
    const container = await renderFrontpage();
    expect(screen.getByRole("button", { name: /^Daily/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Moderation/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /^Daily/ }));
    expect(await screen.findByText("Scene A")).toBeInTheDocument();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    // the plain daily run has no moderation box (that belongs to Moderation)
    expect(screen.queryByText("Moderation verdict")).not.toBeInTheDocument();
    expect(container.querySelector(".stage")).toBeInTheDocument();
  });

  test("the dev instance offers Moderation with the queue count", async () => {
    payload = { ...MANIFEST, moderation: true };
    await renderFrontpage();
    expect(
      screen.getByRole("button", { name: /^Moderation/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("2 scenes waiting for a verdict"),
    ).toBeInTheDocument();
  });

  test("an empty dev queue says there is nothing to review", async () => {
    payload = { version: 2, date: "2026-09-10", scenes: [], moderation: true };
    await renderFrontpage();
    expect(screen.getByText("Nothing to review right now")).toBeInTheDocument();
  });

  test("Daily on the dev instance plays without the moderation box", async () => {
    payload = { ...MANIFEST, moderation: true };
    await renderGame("daily");
    expect(screen.queryByText("Moderation verdict")).not.toBeInTheDocument();
  });

  test("focus lands on the first mode button", async () => {
    await renderFrontpage();
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("button", { name: /^Daily/ }),
      ),
    );
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

/**
 * Would a page unload prompt the user right now? Dispatches the real event
 * the browser fires on reload/close; `defaultPrevented` is what makes a
 * browser show its prompt, and the app never checks the run state in the
 * handler itself, so a `false` here also proves the listener is gone.
 */
function unloadWarns(): boolean {
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
}

describe("reload guard (#1225)", () => {
  test("the frontpage and the empty state never warn", async () => {
    // An empty dev queue: the frontpage still renders, and the mode leads to
    // the empty state instead of a run.
    payload = { version: 2, date: "2026-09-10", scenes: [], moderation: true };
    await renderFrontpage();
    expect(unloadWarns()).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: /^Moderation/ }));
    await screen.findByText("Moderation queue is empty");
    expect(unloadWarns()).toBe(false);
  });

  test("no warning before the first answer", async () => {
    await renderGame();
    expect(unloadWarns()).toBe(false);
  });

  test("the guard arms once a scene is answered", async () => {
    const container = await renderGame();
    clickPhoto(container, 0.5, 0.5);
    await act(async () => {});
    expect(unloadWarns()).toBe(true);
  });

  test("a moderation run warns too; a lost verdict is the same loss", async () => {
    payload = { ...MANIFEST, moderation: true };
    const container = await renderGame("moderation");
    clickPhoto(container, 0.5, 0.5);
    await act(async () => {});
    expect(unloadWarns()).toBe(true);
  });

  test("undoing the only guess disarms the guard again", async () => {
    const container = await renderGame();
    // A double click answers and then takes the guess back (see undoGuess),
    // so there is no score left to lose and no reason to warn anymore.
    clickPhoto(container, 0.5, 0.5);
    fireEvent.doubleClick(overlay(container), {
      clientX: 0.5 * RECT.width,
      clientY: 0.5 * RECT.height,
    });
    await act(async () => {});
    expect(unloadWarns()).toBe(false);
  });

  test("the warning stays armed across scenes and drops on the end screen", async () => {
    const container = await renderGame();
    clickPhoto(container, 0.5, 0.5);
    fireEvent.click(screen.getByRole("button", { name: "Next →" }));
    await act(async () => {});
    expect(unloadWarns()).toBe(true);

    clickPhoto(container, 0.95, 0.95);
    fireEvent.click(screen.getByRole("button", { name: "Results →" }));
    await act(async () => {});
    expect(screen.getByText("Mission complete")).toBeInTheDocument();
    // Unregistering is the only thing that can silence the handler, so this
    // asserts the effect cleanup ran when the run ended.
    expect(unloadWarns()).toBe(false);
  });

  test("in-app navigation to the frontpage does not warn", async () => {
    const container = await renderGame();
    clickPhoto(container, 0.5, 0.5);
    await act(async () => {});

    // #1220/#1223: dropping the path (Back to the frontpage) is an in-app
    // move; no unload happens, so the guard stays quiet.
    act(() => {
      history.replaceState(null, "", "/");
      window.dispatchEvent(new Event("popstate"));
    });
    expect(await screen.findByTestId("mode-daily")).toBeInTheDocument();
    expect(unloadWarns()).toBe(false);
  });

  test("the gallery link is a deliberate unload and stays quiet", async () => {
    payload = { ...MANIFEST, moderation: true };
    const container = await renderGame();
    clickPhoto(container, 0.5, 0.5);
    await act(async () => {});
    expect(unloadWarns()).toBe(true); // sanity: the guard is armed

    fireEvent.click(screen.getByTestId("gallery-menu"));
    expect(unloadWarns()).toBe(false);
  });

  /**
   * Click an in-app link rendered into the live document (#1230): the mode
   * links (#1228) and the way back to the frontpage (#1220) are ordinary
   * anchors that replace the page, like the gallery link, so one
   * document-level click check covers them all. `init` carries the click
   * details (modifiers, target of the click).
   */
  function clickInAppLink(init: MouseEventInit = {}): void {
    const link = document.createElement("a");
    link.href = "/moderation";
    link.textContent = "Moderation";
    document.body.append(link);
    fireEvent.click(link, init);
    link.remove();
  }

  test("a mode link mid-run is a deliberate unload and stays quiet (#1230)", async () => {
    const container = await renderGame();
    clickPhoto(container, 0.5, 0.5);
    await act(async () => {});
    expect(unloadWarns()).toBe(true); // sanity: the guard is armed

    clickInAppLink();
    expect(unloadWarns()).toBe(false);
  });

  test("clicks that keep the page running leave the guard armed (#1230)", async () => {
    const container = await renderGame();
    clickPhoto(container, 0.5, 0.5);
    await act(async () => {});

    // A new tab or window leaves this document running, and a modified click
    // does the same; marking either would disarm the warning for the rest of
    // the run.
    const blank = document.createElement("a");
    blank.href = "/gallery/";
    blank.target = "_blank";
    document.body.append(blank);
    fireEvent.click(blank);
    blank.remove();
    expect(unloadWarns()).toBe(true);

    clickInAppLink({ ctrlKey: true });
    expect(unloadWarns()).toBe(true);

    // A click that is not on a link never unloads the page either.
    fireEvent.click(screen.getByRole("button", { name: /Show hint/ }));
    expect(unloadWarns()).toBe(true);
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
    const container = await renderGame("moderation");
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
    const container = await renderGame("moderation");
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

describe("gallery menu (dev instance, #1204)", () => {
  test("the dev instance links to the image overview", async () => {
    payload = { ...MANIFEST, moderation: true };
    await renderFrontpage();
    const menu = screen.getByRole("link", { name: "☰ Gallery" });
    expect(menu).toHaveAttribute("href", "gallery/");
    expect(menu).toHaveAttribute("title", "Overview of all images");
  });

  test("the dev instance keeps the gallery link while playing", async () => {
    payload = { ...MANIFEST, moderation: true };
    await renderGame("moderation");
    expect(screen.getByRole("link", { name: "☰ Gallery" })).toHaveAttribute(
      "href",
      "gallery/",
    );
  });

  test("the static prod build has no gallery link", async () => {
    // GitHub Pages ships the same bundle without the queue API's moderation
    // flag; the gallery only exists on the dev instance, so no dead link.
    await renderFrontpage();
    expect(screen.queryByRole("link", { name: /Gallery/ })).toBeNull();
  });

  test("the empty moderation queue still offers the way to the gallery", async () => {
    payload = { version: 2, date: "2026-09-10", scenes: [], moderation: true };
    await renderModerationEmpty();
    expect(screen.getByRole("link", { name: "☰ Gallery" })).toHaveAttribute(
      "href",
      "gallery/",
    );
  });
});

describe("empty moderation queue (#1202)", () => {
  const EMPTY = {
    version: 2,
    date: "2026-09-10",
    scenes: [],
    moderation: true,
  };

  test("shows a calm empty state instead of the game shell", async () => {
    payload = EMPTY;
    const container = await renderModerationEmpty();

    expect(
      screen.getByText(/New scenes appear after the daily generation run/),
    ).toBeInTheDocument();
    // no game shell: no progress counter, no photo stage, no dead controls
    expect(screen.queryByText(/\d+ \/ \d+/)).not.toBeInTheDocument();
    expect(container.querySelector(".stage")).toBeNull();
    expect(screen.queryByRole("button", { name: /Show hint/ })).toBeNull();
  });

  test("a queue serialized as null counts as empty too", async () => {
    payload = { ...EMPTY, scenes: null };
    await renderModerationEmpty();
    expect(screen.getByText("Moderation queue is empty")).toBeInTheDocument();
  });

  test("Reload re-fetches and plays the queue once it is filled", async () => {
    payload = EMPTY;
    await renderModerationEmpty();

    payload = { ...MANIFEST, moderation: true };
    fireEvent.click(screen.getByRole("button", { name: "Reload" }));

    expect(await screen.findByText("Scene A")).toBeInTheDocument();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
  });

  test("a non-empty queue never enters the empty state", async () => {
    await renderGame();
    expect(
      screen.queryByText("Moderation queue is empty"),
    ).not.toBeInTheDocument();
  });
});

describe("on-demand generation from the empty queue (#1210)", () => {
  const EMPTY = {
    version: 2,
    date: "2026-09-10",
    scenes: [],
    moderation: true,
  };

  test("shows the buffer depth and offers Generate more", async () => {
    payload = EMPTY;
    generateStatus = {
      state: "idle",
      running: false,
      buffer: 7,
      count: 0,
      planned: 0,
      added: 0,
      failed: 0,
      imageCalls: 0,
    };
    await renderModerationEmpty();
    expect(
      await screen.findByText("7 scenes ready for the daily"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Generate more" }),
    ).not.toBeDisabled();
  });

  test("starting a run shows the progress, then reloads when scenes land", async () => {
    payload = EMPTY;
    await renderModerationEmpty();

    const running = {
      state: "running",
      running: true,
      buffer: 0,
      count: 5,
      planned: 5,
      added: 1,
      failed: 0,
      imageCalls: 2,
    };
    generateStatus = running;
    startResponse = running;
    fireEvent.click(
      await screen.findByRole("button", { name: "Generate more" }),
    );
    await waitFor(() => expect(startCalls).toBe(1));

    expect(
      await screen.findByText("Generating… 1 / 5", {}, { timeout: 4000 }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Generating…" })).toBeDisabled();

    // The run finishes with two new scenes: the poll reloads the manifest and
    // the game starts playing them.
    payload = { ...MANIFEST, moderation: true };
    generateStatus = {
      state: "done",
      running: false,
      buffer: 2,
      count: 5,
      planned: 5,
      added: 2,
      failed: 0,
      imageCalls: 4,
    };
    expect(
      await screen.findByText("Scene A", {}, { timeout: 4000 }),
    ).toBeInTheDocument();
  });

  test("a refused start surfaces the server message", async () => {
    payload = EMPTY;
    startFailure = {
      status: 409,
      detail: "a generation run is already in progress",
    };
    await renderModerationEmpty();
    fireEvent.click(
      await screen.findByRole("button", { name: "Generate more" }),
    );
    expect(
      await screen.findByText(/a generation run is already in progress/),
    ).toBeInTheDocument();
    // the button stays usable so Evan can retry
    expect(
      screen.getByRole("button", { name: "Generate more" }),
    ).not.toBeDisabled();
  });

  test("a failed run shows the error the generator reported", async () => {
    payload = EMPTY;
    generateStatus = {
      state: "error",
      running: false,
      buffer: 0,
      count: 5,
      planned: 0,
      added: 0,
      failed: 0,
      imageCalls: 0,
      error: "OPENROUTER_API_KEY not set",
    };
    await renderModerationEmpty();
    expect(
      await screen.findByText("OPENROUTER_API_KEY not set"),
    ).toBeInTheDocument();
  });
});

describe("mode URLs (#1223)", () => {
  /** Render at a deep-linked path and wait for the first scene. */
  async function renderDeepLink(path: string): Promise<HTMLElement> {
    history.replaceState(null, "", path);
    const { container } = render(<App />);
    await screen.findByText("Scene A");
    await act(async () => {});
    return container;
  }

  test("the frontpage is the default address", async () => {
    await renderFrontpage();
    expect(window.location.pathname).toBe("/");
    expect(screen.getByRole("button", { name: /^Daily/ })).toBeInTheDocument();
  });

  test("a /daily deep link starts the daily run", async () => {
    await renderDeepLink("/daily");
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    // no frontpage in between: the path named the mode
    expect(screen.queryByRole("button", { name: /^Daily/ })).toBeNull();
    expect(window.location.pathname).toBe("/daily");
  });

  test("a /moderation deep link starts the queue on the dev instance", async () => {
    payload = { ...MANIFEST, moderation: true };
    const container = await renderDeepLink("/moderation");
    clickPhoto(container, 0.5, 0.5);
    expect(screen.getByText("Moderation verdict")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/moderation");
  });

  test("picking Daily pushes /daily onto the history", async () => {
    await renderFrontpage();
    fireEvent.click(screen.getByRole("button", { name: /^Daily/ }));
    await screen.findByText("Scene A");
    expect(window.location.pathname).toBe("/daily");
  });

  test("picking Moderation pushes /moderation onto the history", async () => {
    payload = { ...MANIFEST, moderation: true };
    await renderFrontpage();
    fireEvent.click(screen.getByRole("button", { name: /^Moderation/ }));
    await screen.findByText("Scene A");
    expect(window.location.pathname).toBe("/moderation");
  });

  test("popstate moves between the frontpage and a mode", async () => {
    payload = { ...MANIFEST, moderation: true };
    await renderFrontpage();

    // Forward to a mode (a history entry the app pushed).
    history.replaceState(null, "", "/moderation");
    fireEvent(window, new Event("popstate"));
    expect(await screen.findByText("Scene A")).toBeInTheDocument();

    // Back to the frontpage.
    history.replaceState(null, "", "/");
    fireEvent(window, new Event("popstate"));
    expect(
      await screen.findByRole("button", { name: /^Moderation/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Scene A")).toBeNull();
  });

  test("a /moderation load on prod degrades to the frontpage", async () => {
    // The static prod manifest carries no dev flag, so there is no queue mode.
    history.replaceState(null, "", "/moderation");
    render(<App />);
    expect(
      await screen.findByRole("button", { name: /^Daily/ }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Moderation/ })).toBeNull();
    // the dead path is dropped so the URL matches the screen
    expect(window.location.pathname).toBe("/");
  });

  test("an unknown path falls back to the frontpage", async () => {
    history.replaceState(null, "", "/no-such-mode");
    render(<App />);
    expect(
      await screen.findByRole("button", { name: /^Daily/ }),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
  });

  test("the 404 shim's stashed path is restored on a hard load", async () => {
    // GitHub Pages serves 404.html for /daily: it stashes the path and sends
    // the browser to the base; the app must replaceState the path back.
    sessionStorage.setItem("anomalyguessr:redirect", "/daily");
    const { container } = render(<App />);
    await screen.findByText("Scene A");
    await act(async () => {});
    expect(window.location.pathname).toBe("/daily");
    expect(container.querySelector("#scene-title")?.textContent).toBe(
      "Scene A",
    );
    // the stash is consumed, not replayed on the next render
    expect(sessionStorage.getItem("anomalyguessr:redirect")).toBeNull();
  });
});

afterEach(() => {
  globalThis.fetch = fetch;
});
