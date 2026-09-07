import { fitInto, panBy, type ViewState, zoomAt } from "./layout";
import {
  DIFFICULTY_LABELS,
  type Manifest,
  parseManifest,
  type Scene,
} from "./manifest";
import { clickDistance, scoreFor, type Verdict, verdictFor } from "./scoring";

interface RunState {
  queue: Scene[];
  index: number;
  hintsUsed: number;
  totalScore: number;
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
  sceneDifficulty: $("#scene-difficulty"),
  sceneProgress: $("#scene-progress"),
  img: $("#photo-img") as HTMLImageElement,
  overlay: $("#overlay"),
  hintBtn: $("#hint-btn") as HTMLButtonElement,
  hintText: $("#hint-text"),
  result: $("#result"),
  compareBtn: $("#compare-btn") as HTMLButtonElement,
  nextBtn: $("#next-btn") as HTMLButtonElement,
  endText: $("#end-text"),
  restartBtn: $("#restart-btn") as HTMLButtonElement,
  loadError: $("#load-error"),
};

const DESKTOP_LAYOUT = "(min-width: 900px) and (min-height: 560px)";

const VERDICT_COPY: Record<Verdict, { headline: string; note: string }> = {
  saved: {
    headline: "Zeitlinie gesichert!",
    note: "Punktgenau erkannt. Die Geschichte ist gerettet.",
  },
  warm: {
    headline: "Fast!",
    note: "Die Anomalie war ganz in der Nähe.",
  },
  miss: {
    headline: "Daneben.",
    note: "Die Zeitlinie flackert bedenklich …",
  },
};

let manifest: Manifest | null = null;
let state: RunState = {
  queue: [],
  index: 0,
  hintsUsed: 0,
  totalScore: 0,
  answered: false,
  originalView: false,
};
let view: ViewState = { scale: 1, x: 0, y: 0 };
let dragMoved = false;
let dragStart: { x: number; y: number; view: ViewState } | null = null;
const activePointers = new Map<number, { x: number; y: number }>();
let pinchLast: { dist: number; midX: number; midY: number } | null = null;
let lastScoreDelta = 0;

function shuffle<T>(items: T[]): T[] {
  const copy = [...items];
  for (let i = copy.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    const tmp = copy[i];
    copy[i] = copy[j] as T;
    copy[j] = tmp as T;
  }
  return copy;
}

function scene(): Scene {
  const s = state.queue[state.index];
  if (!s) throw new Error("no scene at index");
  return s;
}

function startRun(): void {
  if (!manifest) return;
  state = {
    queue: shuffle(manifest.scenes),
    index: 0,
    hintsUsed: 0,
    totalScore: 0,
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
  els.sceneDifficulty.textContent = DIFFICULTY_LABELS[s.difficulty];
  els.sceneDifficulty.className = `badge ${s.difficulty}`;
  els.sceneProgress.textContent = `${state.index + 1} / ${state.queue.length}`;
  els.img.removeAttribute("style");
  els.img.src = s.image;
  els.img.alt = `Historisches Foto: ${s.title} (${s.place}, ${s.year})`;
  els.hintText.textContent = "";
  els.hintBtn.disabled = false;
  els.hintBtn.textContent = "💡 Tipp zeigen";
  els.result.className = "result";
  els.result.innerHTML = "";
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

/** Photo base box in view coordinates (transform-independent layout box). */
function photoBase(): {
  left: number;
  top: number;
  width: number;
  height: number;
} {
  const stageRect = els.stage.getBoundingClientRect();
  return {
    left: stageRect.left + els.photo.offsetLeft,
    top: stageRect.top + els.photo.offsetTop,
    width: els.photo.offsetWidth,
    height: els.photo.offsetHeight,
  };
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
    state.totalScore = Math.max(0, state.totalScore - lastScoreDelta);
    lastScoreDelta = 0;
    els.overlay.classList.remove("waiting");
    clearMarkers();
    els.result.className = "result";
    els.result.innerHTML = "";
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
  lastScoreDelta = score;
  state.totalScore += score;
  const verdict = verdictFor(score);
  addMarker("click", nx, ny);
  addMarker("answer", s.answer.x, s.answer.y, s.answer.r);
  const used = state.hintsUsed;
  const penaltyNote =
    used === 0
      ? "ohne Tipp"
      : used === 1
        ? "1 Tipp genutzt"
        : `${used} Tipps genutzt`;
  const copy = VERDICT_COPY[verdict];
  els.result.className = `result ${verdict}`;
  els.result.innerHTML = `
    <p class="headline">${copy.headline}</p>
    <p class="score"><strong>${score} Punkte</strong> · ${penaltyNote}</p>
    <p>${copy.note}</p>
    <p>🔍 Die Anomalie war eine <strong>${s.anomaly}</strong>. Genaue Stelle: ${s.hints[2]}</p>
  `;
  els.compareBtn.hidden = false;
  els.nextBtn.hidden = false;
  const last = state.index >= state.queue.length - 1;
  els.nextBtn.textContent = last ? "Ergebnis →" : "Weiter →";
  els.nextBtn.focus();
}

function onHint(): void {
  const s = scene();
  if (state.hintsUsed >= 3 || state.answered) return;
  state.hintsUsed += 1;
  const hint = s.hints[state.hintsUsed - 1];
  els.hintText.textContent = `Tipp ${state.hintsUsed}/3: ${hint}`;
  if (state.hintsUsed >= 3) {
    els.hintBtn.disabled = true;
    els.hintBtn.textContent = "💡 Keine Tipps mehr";
  } else {
    els.hintBtn.textContent = `💡 Tipp zeigen (${state.hintsUsed}/3)`;
  }
}

function toggleCompare(): void {
  const s = scene();
  state.originalView = !state.originalView;
  els.img.src = state.originalView ? s.original : s.image;
  els.compareBtn.textContent = state.originalView
    ? "🖼️ Bearbeitetes Bild"
    : "📷 Original anzeigen";
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
  const avg = Math.round(state.totalScore / n);
  els.endText.textContent =
    `${state.totalScore} von ${n * 100} Punkten (Ø ${avg}) ` +
    `bei ${n} Szenen. Der Zeitfluss bleibt erhalten – vorerst.`;
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
  const desktop = window.matchMedia(DESKTOP_LAYOUT);
  if (!desktop.matches) {
    els.img.style.width = "";
    els.img.style.height = "";
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
