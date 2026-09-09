import { DAILY_COUNT, dateFromKey, dateLabel, pickDaily } from "./daily";
import { fitInto, panBy, type ViewState, zoomAt } from "./layout";
import { type Manifest, parseManifest, type Scene } from "./manifest";
import { clickDistance, scoreFor, type Verdict, verdictFor } from "./scoring";

interface RunState {
  /** Today's deterministic daily set. */
  queue: Scene[];
  index: number;
  hintsUsed: number;
  /** Per-scene scores, aligned with queue order (-1 until answered). */
  scores: number[];
  answered: boolean;
  originalView: boolean;
}

function $<T extends HTMLElement>(selector: string): T {
  const el = document.querySelector<T>(selector);
  if (!el) throw new Error(`missing element: ${selector}`);
  return el;
}

const els = {
  game: $("#game"),
  end: $("#end"),
  stage: $(".stage"),
  photo: $("#photo"),
  sceneTitle: $("#scene-title"),
  scenePlace: $("#scene-place"),
  sceneDesc: $("#scene-desc"),
  sceneProgress: $("#scene-progress"),
  img: $("#photo-img") as HTMLImageElement,
  overlay: $("#overlay"),
  hintBtn: $("#hint-btn") as HTMLButtonElement,
  hintText: $("#hint-text"),
  result: $("#result"),
  why: $("#why"),
  compareBtn: $("#compare-btn") as HTMLButtonElement,
  nextBtn: $("#next-btn") as HTMLButtonElement,
  endDate: $("#end-date"),
  endList: $("#end-list"),
  endText: $("#end-text"),
  restartBtn: $("#restart-btn") as HTMLButtonElement,
  loadError: $("#load-error"),
};

const DESKTOP_LAYOUT = "(min-width: 900px) and (min-height: 560px)";

const VERDICT_COPY: Record<Verdict, { headline: string; note: string }> = {
  saved: {
    headline: "Timeline secured!",
    note: "Pinpoint hit. History is safe.",
  },
  warm: {
    headline: "Close!",
    note: "The anomaly was right nearby.",
  },
  miss: {
    headline: "Miss.",
    note: "The timeline is flickering ominously …",
  },
};

let manifest: Manifest | null = null;
/** The quiz day, fixed per session load (local calendar day). */
let quizDay = new Date();
let state: RunState = {
  queue: [],
  index: 0,
  hintsUsed: 0,
  scores: [],
  answered: false,
  originalView: false,
};
let view: ViewState = { scale: 1, x: 0, y: 0 };
let dragMoved = false;
let dragStart: { x: number; y: number; view: ViewState } | null = null;
const activePointers = new Map<number, { x: number; y: number }>();
let pinchLast: { dist: number; midX: number; midY: number } | null = null;

/**
 * Today's set. Version-2 manifests are pipeline-shipped daily manifests:
 * they already contain exactly the day's 5 scenes in play order, so the
 * frontend plays them as-is. Version-1 manifests are the legacy pool (all
 * known scenes); for those the frontend falls back to the deterministic
 * date-seeded selection so the game keeps working while a pool manifest is
 * live (transition period / manual deploys).
 */
function dailySet(): Scene[] {
  if (!manifest) return [];
  if (manifest.version === 1)
    return pickDaily(manifest.scenes, quizDay, DAILY_COUNT);
  return manifest.scenes;
}

/** English end-screen label for the quiz day (manifest date beats browser). */
function quizLabel(): string {
  if (manifest?.version === 2 && manifest.date)
    return dateLabel(dateFromKey(manifest.date));
  return dateLabel(quizDay);
}

function scene(): Scene {
  const s = state.queue[state.index];
  if (!s) throw new Error("no scene at index");
  return s;
}

function startRun(): void {
  if (!manifest) return;
  quizDay = new Date();
  state = {
    queue: dailySet(),
    index: 0,
    hintsUsed: 0,
    scores: [],
    answered: false,
    originalView: false,
  };
  els.end.hidden = true;
  els.game.hidden = false;
  renderScene();
}

function renderScene(): void {
  const s = scene();
  state.hintsUsed = 0;
  state.answered = false;
  state.originalView = false;
  view = { scale: 1, x: 0, y: 0 };
  dragMoved = false;
  dragStart = null;
  activePointers.clear();
  pinchLast = null;
  applyTransform();
  els.game.hidden = false;
  els.end.hidden = true;
  els.sceneTitle.textContent = s.title;
  els.scenePlace.textContent = `${s.place}, ${s.year}`;
  els.sceneDesc.textContent = s.description;
  els.sceneProgress.textContent = `${state.index + 1} / ${state.queue.length}`;
  els.img.removeAttribute("style");
  els.img.src = s.image;
  els.img.alt = `Historical photo: ${s.title} (${s.place}, ${s.year})`;
  els.hintText.textContent = "";
  els.hintBtn.disabled = false;
  els.hintBtn.textContent = "💡 Show hint";
  els.result.className = "result";
  els.result.innerHTML = "";
  els.why.hidden = true;
  els.why.innerHTML = "";
  els.compareBtn.hidden = true;
  els.nextBtn.hidden = true;
  els.overlay.classList.remove("waiting");
  clearMarkers();
  fitPhotoToStage();
}

function clearMarkers(): void {
  for (const m of els.overlay.querySelectorAll(".marker")) m.remove();
}

function addMarker(
  type: "click" | "answer",
  x: number,
  y: number,
  radius?: number,
): void {
  const m = document.createElement("div");
  m.className = `marker ${type}`;
  m.style.left = `${x * 100}%`;
  m.style.top = `${y * 100}%`;
  if (type === "answer" && radius) {
    m.style.width = `${radius * 200}%`;
    m.style.height = `${radius * 200}%`;
  }
  els.overlay.append(m);
}

/**
 * Untransformed image box in viewport coordinates. The zoom transform
 * (translate + scale, origin 0 0) is applied to the #photo wrapper; the image
 * layout box itself never moves, but getBoundingClientRect() would include
 * the transform. The layout box only changes when the image loads or the
 * viewport resizes, and both paths funnel through fitPhotoToStage, so we
 * re-measure there (at scale 1) and keep the cached box for click/zoom math.
 */
let baseBox = { left: 0, top: 0, width: 0, height: 0 };

function photoBase(): {
  left: number;
  top: number;
  width: number;
  height: number;
} {
  return baseBox;
}

function measureBase(): void {
  const r = els.img.getBoundingClientRect();
  baseBox = { left: r.left, top: r.top, width: r.width, height: r.height };
}

function applyTransform(): void {
  els.photo.style.transformOrigin = "0 0";
  els.photo.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
  els.overlay.classList.toggle("zoomed", view.scale > 1.001);
}

function onOverlayClick(e: MouseEvent): void {
  if (state.answered) return;
  if (dragMoved) {
    // the gesture was a pan, not a guess
    dragMoved = false;
    return;
  }
  const base = photoBase();
  if (base.width === 0 || base.height === 0) return;
  // view-space cursor -> image-local -> normalized (score stays in original
  // image coordinates; only the display is zoomed/panned)
  const px = e.clientX - base.left;
  const py = e.clientY - base.top;
  const ux = (px - view.x) / view.scale;
  const uy = (py - view.y) / view.scale;
  const nx = ux / base.width;
  const ny = uy / base.height;
  if (nx < 0 || nx > 1 || ny < 0 || ny > 1) return;
  resolve(nx, ny);
}

function zoomAtPoint(cursorX: number, cursorY: number, factor: number): void {
  const base = photoBase();
  if (base.width === 0) return;
  const px = cursorX - base.left;
  const py = cursorY - base.top;
  view = zoomAt(view, base.width, base.height, px, py, factor);
  applyTransform();
}

function resetZoom(): void {
  view = { scale: 1, x: 0, y: 0 };
  applyTransform();
}

function onWheel(e: WheelEvent): void {
  if (state.answered) return;
  e.preventDefault();
  const factor = Math.exp(-e.deltaY * 0.0016);
  zoomAtPoint(e.clientX, e.clientY, factor);
}

function onDblClick(e: MouseEvent): void {
  e.preventDefault();
  if (view.scale > 1.001) {
    resetZoom();
    return;
  }
  // a double-click means "zoom in here", not "guess twice": undo the first
  // click's guess resolution, then zoom
  if (state.answered) {
    state.answered = false;
    state.scores[state.index] = -1;
    els.overlay.classList.remove("waiting");
    clearMarkers();
    els.result.className = "result";
    els.result.innerHTML = "";
    els.why.hidden = true;
    els.why.innerHTML = "";
    els.compareBtn.hidden = true;
    els.nextBtn.hidden = true;
  }
  zoomAtPoint(e.clientX, e.clientY, 3);
}

function pointerPos(e: PointerEvent): { x: number; y: number } {
  return { x: e.clientX, y: e.clientY };
}

function onPointerDown(e: PointerEvent): void {
  if (state.answered) return;
  activePointers.set(e.pointerId, pointerPos(e));
  if (activePointers.size === 1) {
    dragStart = { x: e.clientX, y: e.clientY, view: { ...view } };
    dragMoved = false;
    try {
      els.overlay.setPointerCapture(e.pointerId);
    } catch {
      // pointer capture is best-effort
    }
  } else if (activePointers.size === 2) {
    dragStart = null;
    const pts = [...activePointers.values()];
    const a = pts[0];
    const b = pts[1];
    if (a && b) {
      pinchLast = {
        dist: Math.hypot(a.x - b.x, a.y - b.y),
        midX: (a.x + b.x) / 2,
        midY: (a.y + b.y) / 2,
      };
    }
  }
}

function onPointerMove(e: PointerEvent): void {
  if (state.answered) return;
  if (!activePointers.has(e.pointerId)) return;
  activePointers.set(e.pointerId, pointerPos(e));
  const pts = [...activePointers.values()];
  if (pts.length === 2 && pinchLast) {
    const a = pts[0];
    const b = pts[1];
    if (!a || !b) return;
    const dist = Math.hypot(a.x - b.x, a.y - b.y);
    const midX = (a.x + b.x) / 2;
    const midY = (a.y + b.y) / 2;
    if (pinchLast.dist > 0) {
      const base = photoBase();
      view = zoomAt(
        view,
        base.width,
        base.height,
        pinchLast.midX - base.left,
        pinchLast.midY - base.top,
        dist / pinchLast.dist,
      );
      // follow the pinch midpoint's movement
      const dx = midX - pinchLast.midX;
      const dy = midY - pinchLast.midY;
      view = panBy(view, dx, dy, base.width, base.height);
      applyTransform();
    }
    pinchLast = { dist, midX, midY };
    return;
  }
  if (dragStart && pts.length === 1) {
    const dx = e.clientX - dragStart.x;
    const dy = e.clientY - dragStart.y;
    if (Math.hypot(dx, dy) > 4) dragMoved = true;
    if (view.scale > 1 || dragMoved) {
      const base = photoBase();
      view = panBy(dragStart.view, dx, dy, base.width, base.height);
      applyTransform();
    }
  }
}

function onPointerUp(e: PointerEvent): void {
  activePointers.delete(e.pointerId);
  pinchLast = null;
  if (activePointers.size < 2) pinchLast = null;
  if (activePointers.size === 0) dragStart = null;
}

function onPointerCancel(e: PointerEvent): void {
  activePointers.delete(e.pointerId);
  pinchLast = null;
  if (activePointers.size === 0) dragStart = null;
}

function resolve(nx: number, ny: number): void {
  const s = scene();
  state.answered = true;
  els.overlay.classList.add("waiting");
  const distance = clickDistance({ x: nx, y: ny }, s.answer);
  const score = scoreFor(distance, s.answer.r, state.hintsUsed);
  state.scores[state.index] = score;
  const verdict = verdictFor(score);
  addMarker("click", nx, ny);
  addMarker("answer", s.answer.x, s.answer.y, s.answer.r);
  const used = state.hintsUsed;
  const penaltyNote =
    used === 0 ? "no hints" : used === 1 ? "1 hint used" : `${used} hints used`;
  const copy = VERDICT_COPY[verdict];
  els.result.className = `result ${verdict}`;
  els.result.innerHTML = `
    <p class="headline">${copy.headline}</p>
    <p class="score"><strong>${score} points</strong> · ${penaltyNote}</p>
    <p>${copy.note}</p>
    <p>🔍 The anomaly was: <strong>${s.anomaly}</strong>. Exact spot: ${s.hints[2]}</p>
  `;
  if (s.explanation) renderWhy(s);
  els.compareBtn.hidden = false;
  els.nextBtn.hidden = false;
  const last = state.index >= state.queue.length - 1;
  els.nextBtn.textContent = last ? "Results →" : "Next →";
  els.nextBtn.focus();
}

/**
 * Post-guess reveal: explain WHY the anomaly could not have been in the
 * original photo (per-scene "explanation" + checkable reference links).
 * Legacy scenes without an explanation show nothing.
 */
function renderWhy(s: Scene): void {
  els.why.innerHTML = "";
  const head = document.createElement("p");
  head.className = "why-head";
  head.textContent = "Why?";
  els.why.append(head);
  const body = document.createElement("p");
  body.className = "why-text";
  body.textContent = s.explanation ?? "";
  els.why.append(body);
  if (s.references && s.references.length > 0) {
    const ul = document.createElement("ul");
    ul.className = "why-refs";
    for (const ref of s.references) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = ref.url;
      a.textContent = ref.label;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      li.append(a);
      ul.append(li);
    }
    els.why.append(ul);
  }
  els.why.hidden = false;
}

function onHint(): void {
  const s = scene();
  if (state.hintsUsed >= 3 || state.answered) return;
  state.hintsUsed += 1;
  const hint = s.hints[state.hintsUsed - 1];
  els.hintText.textContent = `Hint ${state.hintsUsed}/3: ${hint}`;
  if (state.hintsUsed >= 3) {
    els.hintBtn.disabled = true;
    els.hintBtn.textContent = "💡 No hints left";
  } else {
    els.hintBtn.textContent = `💡 Show hint (${state.hintsUsed}/3)`;
  }
}

function toggleCompare(): void {
  const s = scene();
  state.originalView = !state.originalView;
  els.img.src = state.originalView ? s.original : s.image;
  els.compareBtn.textContent = state.originalView
    ? "🖼️ Edited image"
    : "📷 Show original";
}

function nextScene(): void {
  state.index += 1;
  if (state.index >= state.queue.length) {
    showEnd();
  } else {
    renderScene();
  }
}

function showEnd(): void {
  els.game.hidden = true;
  els.end.hidden = false;
  const n = state.queue.length;
  const scores = state.queue.map((_, i) => state.scores[i] ?? 0);
  const total = scores.reduce((sum, v) => sum + v, 0);
  const avg = Math.round(total / n);
  els.endDate.textContent = `Daily quiz · ${quizLabel()}`;
  els.endList.innerHTML = "";
  for (let i = 0; i < n; i++) {
    const s = state.queue[i];
    if (!s) continue;
    const li = document.createElement("li");
    li.textContent = `${i + 1}. ${s.title} · ${scores[i] ?? 0} pts`;
    els.endList.append(li);
  }
  els.endText.textContent =
    `${total} of ${n * 100} points (avg ${avg}) across ${n} scenes. ` +
    `The timeline holds, for now.`;
  els.restartBtn.focus();
}

/**
 * Fit the current photo into the stage box on the laptop layout. The stage
 * has a fixed viewport-derived size there (CSS media query), so the image
 * needs explicit pixel dimensions to keep the click overlay aligned with the
 * displayed photo (a letterboxed object-fit would break the coordinate math
 * in onOverlayClick). On the stacked layout the stage is content-sized and
 * the plain CSS max-width constraint applies instead.
 */
function fitPhotoToStage(): void {
  // re-fit changes the layout box, which invalidates a cached base and any
  // active pan/zoom; reset to identity so the measurement is untransformed
  if (view.scale !== 1 || view.x !== 0 || view.y !== 0) resetZoom();
  const desktop = window.matchMedia(DESKTOP_LAYOUT);
  if (!desktop.matches) {
    els.img.style.width = "";
    els.img.style.height = "";
    if (els.img.naturalWidth) measureBase();
    return;
  }
  if (!els.img.naturalWidth) return;
  const w = els.stage.clientWidth;
  const h = els.stage.clientHeight;
  if (w <= 0 || h <= 0) return;
  // keep the 1px photo border inside the stage
  const fit = fitInto(
    w - 2,
    h - 2,
    els.img.naturalWidth,
    els.img.naturalHeight,
  );
  if (!fit) return;
  els.img.style.width = `${fit.width}px`;
  els.img.style.height = `${fit.height}px`;
  measureBase();
}

async function init(): Promise<void> {
  els.hintBtn.addEventListener("click", onHint);
  els.overlay.addEventListener("click", onOverlayClick);
  els.overlay.addEventListener("dblclick", onDblClick);
  els.overlay.addEventListener("wheel", onWheel, { passive: false });
  els.overlay.addEventListener("pointerdown", onPointerDown);
  els.overlay.addEventListener("pointermove", onPointerMove);
  els.overlay.addEventListener("pointerup", onPointerUp);
  els.overlay.addEventListener("pointercancel", onPointerCancel);
  els.compareBtn.addEventListener("click", toggleCompare);
  els.nextBtn.addEventListener("click", nextScene);
  els.restartBtn.addEventListener("click", () => startRun());
  els.img.addEventListener("load", fitPhotoToStage);
  window.addEventListener("resize", () => fitPhotoToStage());
  window
    .matchMedia(DESKTOP_LAYOUT)
    .addEventListener("change", () => fitPhotoToStage());
  try {
    const res = await fetch("scenes/manifest.json");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    manifest = parseManifest(await res.json());
  } catch (err) {
    console.error("manifest load failed", err);
    els.loadError.hidden = false;
    return;
  }
  startRun();
}

init();
