/**
 * DOM-level tests for the post-guess reveal flow (ticket #1147): a correct
 * guess (click inside the answer circle) dissolves the edited photo into the
 * untouched original; misses and warm clicks stay on the edited photo; the
 * compare button toggles the original layer manually.
 *
 * game.ts is a module singleton that renders the current scene on import
 * (init() fetches the manifest), so the tests share one run state and must
 * run in order: scene A (hit) -> scene B (miss + compare) -> restart (warm).
 */

import { beforeAll, describe, expect, test } from "bun:test";
import { isHit } from "../scoring";

const SKELETON = `
<main>
  <section id="game" class="game">
    <div class="meta">
      <span id="scene-title" class="scene-title"></span>
      <span id="scene-place" class="scene-place"></span>
      <span id="scene-progress" class="progress"></span>
    </div>
    <p id="scene-desc" class="scene-desc"></p>
    <div class="stage">
      <div id="photo" class="photo">
        <img id="photo-img" alt="" />
        <img id="photo-orig" class="photo-orig" alt="" aria-hidden="true" />
        <div id="overlay" class="overlay"></div>
      </div>
    </div>
    <p id="img-announce" class="sr-only" aria-live="polite"></p>
    <div class="hud">
      <div id="hint-box" class="hint-box">
        <button id="hint-btn" class="btn" type="button">Show hint</button>
        <p id="hint-text" class="hint-text"></p>
      </div>
      <div id="result" class="result"></div>
      <div id="why" class="why" hidden></div>
      <div class="nav">
        <button id="compare-btn" class="btn ghost" type="button" hidden>
          Show original
        </button>
        <button id="next-btn" class="btn primary" type="button" hidden>Next →</button>
      </div>
    </div>
  </section>
  <section id="end" class="end" hidden>
    <h2>Mission complete</h2>
    <p id="end-date" class="end-date"></p>
    <ol id="end-list" class="end-list"></ol>
    <p id="end-text"></p>
    <button id="restart-btn" class="btn primary" type="button">Play again</button>
  </section>
  <p id="load-error" class="load-error" hidden></p>
</main>`;

const SOURCE = {
  repository: "Test repository",
  fileUrl: "https://example.org/file",
  originalTitle: "Test original",
  date: "1900",
  place: "Dresden",
  license: "Public Domain",
  description: "A test scene.",
};

function scene(id: string, title: string, x: number, y: number, r: number) {
  return {
    id,
    title,
    place: "Dresden",
    year: "1900",
    credit: "Public Domain, via Wikimedia Commons",
    sourceUrl: `https://example.org/${id}`,
    source: SOURCE,
    image: `scenes/${id}.jpg`,
    original: `scenes/${id}-original.jpg`,
    anomaly: "Drone",
    description: `Context for ${title}.`,
    explanation: "Drones did not exist in 1900.",
    answer: { x, y, r },
    hints: ["Attached to a person.", "Right half.", "In the sky: a drone."],
  };
}

const MANIFEST = {
  version: 2,
  date: "2026-09-09",
  scenes: [
    scene("a", "Scene A", 0.5, 0.5, 0.05),
    scene("b", "Scene B", 0.2, 0.8, 0.04),
  ],
};

const $ = <T extends HTMLElement>(selector: string): T => {
  const el = document.querySelector<T>(selector);
  if (!el) throw new Error(`missing element: ${selector}`);
  return el;
};

/** Let happy-dom's async image-fetch error events settle between scenes. */
const settle = (ms = 150): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

let game: typeof import("../game");

beforeAll(async () => {
  document.body.innerHTML = SKELETON;
  globalThis.fetch = async () =>
    ({
      ok: true,
      status: 200,
      json: async () => MANIFEST,
    }) as Response;
  game = await import("../game");
  await settle();
});

describe("reveal to original on a correct guess (#1147)", () => {
  test("a hit inside the answer circle is detected geometrically", () => {
    const s = MANIFEST.scenes[0];
    if (!s) throw new Error("test manifest empty");
    expect(isHit(0, s.answer.r)).toBe(true);
    expect(isHit(s.answer.r * 1.5, s.answer.r)).toBe(false);
  });

  test("scene A: perfect hit crossfades to the original", async () => {
    expect($("#scene-title").textContent).toBe("Scene A");
    const orig = $("#photo-orig");
    const compare = $("#compare-btn");
    const announce = $("#img-announce");
    expect(orig.classList.contains("reveal")).toBe(false);

    game.resolve(0.5, 0.5);
    // The original layer preloads; until its load event the reveal is
    // pending but the compare button already reflects the coming swap.
    expect(compare.hidden).toBe(false);
    expect(compare.textContent).toContain("Edited image");
    expect(orig.classList.contains("reveal")).toBe(false);

    orig.dispatchEvent(new Event("load"));
    expect(orig.classList.contains("reveal")).toBe(true);
    expect($("#photo").classList.contains("correcting")).toBe(true);
    expect(announce.textContent).not.toBe("");
    expect($("#result").className).toContain("saved");
  });

  test("scene B: a miss stays on the edited photo; compare toggles manually", async () => {
    $("#next-btn").click();
    await settle();
    expect($("#scene-title").textContent).toBe("Scene B");
    const orig = $("#photo-orig");
    const compare = $("#compare-btn");
    expect(orig.classList.contains("reveal")).toBe(false);
    expect(orig.classList.contains("show")).toBe(false);

    game.resolve(0.95, 0.95); // far corner: miss
    expect(orig.classList.contains("reveal")).toBe(false);
    expect(compare.textContent).toContain("Show original");
    expect($("#result").className).toContain("miss");

    compare.click();
    expect(orig.classList.contains("show")).toBe(true);
    expect(compare.textContent).toContain("Edited image");

    compare.click();
    expect(orig.classList.contains("show")).toBe(false);
    expect(compare.textContent).toContain("Show original");
  });

  test("restart: a warm click (near miss) does not reveal the original", async () => {
    $("#restart-btn").click();
    await settle();
    expect($("#scene-title").textContent).toBe("Scene A");
    const orig = $("#photo-orig");
    const compare = $("#compare-btn");

    game.resolve(0.7, 0.5); // distance 0.2: warm, outside the answer circle
    expect(orig.classList.contains("reveal")).toBe(false);
    expect(orig.classList.contains("show")).toBe(false);
    expect(compare.textContent).toContain("Show original");
    expect($("#result").className).toContain("warm");
  });
});
