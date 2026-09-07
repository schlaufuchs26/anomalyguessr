import {
  DIFFICULTIES,
  DIFFICULTY_LABELS,
  type Difficulty,
  type Manifest,
  parseManifest,
  type Scene,
} from "./manifest";
import { clickDistance, scoreFor, type Verdict, verdictFor } from "./scoring";

type DifficultyFilter = Difficulty | "alle";

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
  filter: $("#filter"),
  game: $("#game"),
  end: $("#end"),
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
let activeFilter: DifficultyFilter = "alle";
let state: RunState = {
  queue: [],
  index: 0,
  hintsUsed: 0,
  totalScore: 0,
  answered: false,
  originalView: false,
};

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

function startRun(filter: DifficultyFilter): void {
  if (!manifest) return;
  activeFilter = filter;
  const scenes =
    filter === "alle"
      ? manifest.scenes
      : manifest.scenes.filter((s) => s.difficulty === filter);
  state = {
    queue: shuffle(scenes),
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
  els.game.hidden = false;
  els.end.hidden = true;
  els.sceneTitle.textContent = s.title;
  els.scenePlace.textContent = `${s.place}, ${s.year}`;
  els.sceneDifficulty.textContent = DIFFICULTY_LABELS[s.difficulty];
  els.sceneDifficulty.className = `badge ${s.difficulty}`;
  els.sceneProgress.textContent = `${state.index + 1} / ${state.queue.length}`;
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

function onOverlayClick(e: MouseEvent): void {
  if (state.answered) return;
  const rect = els.overlay.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) return;
  const nx = (e.clientX - rect.left) / rect.width;
  const ny = (e.clientY - rect.top) / rect.height;
  if (nx < 0 || nx > 1 || ny < 0 || ny > 1) return;
  resolve(nx, ny);
}

function resolve(nx: number, ny: number): void {
  const s = scene();
  state.answered = true;
  els.overlay.classList.add("waiting");
  const distance = clickDistance({ x: nx, y: ny }, s.answer);
  const score = scoreFor(distance, s.answer.r, state.hintsUsed);
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

function buildFilters(): void {
  if (!manifest) return;
  const counts = new Map<DifficultyFilter, number>();
  counts.set("alle", manifest.scenes.length);
  for (const d of DIFFICULTIES)
    counts.set(d, manifest.scenes.filter((s) => s.difficulty === d).length);
  const chips: DifficultyFilter[] = ["alle", ...DIFFICULTIES];
  for (const filter of chips) {
    const btn = document.createElement("button");
    const count = counts.get(filter) ?? 0;
    const label = filter === "alle" ? "Alle" : DIFFICULTY_LABELS[filter];
    btn.type = "button";
    btn.className = `chip ${filter === "alle" ? "" : filter}`;
    btn.setAttribute("aria-pressed", "false");
    btn.textContent = `${label} (${count})`;
    btn.addEventListener("click", () => {
      for (const c of els.filter.querySelectorAll(".chip")) {
        c.setAttribute("aria-pressed", c === btn ? "true" : "false");
      }
      startRun(filter);
    });
    els.filter.append(btn);
  }
  const all = els.filter.querySelector(".chip");
  all?.setAttribute("aria-pressed", "true");
}

async function init(): Promise<void> {
  els.hintBtn.addEventListener("click", onHint);
  els.overlay.addEventListener("click", onOverlayClick);
  els.compareBtn.addEventListener("click", toggleCompare);
  els.nextBtn.addEventListener("click", nextScene);
  els.restartBtn.addEventListener("click", () => startRun(activeFilter));
  try {
    const res = await fetch("scenes/manifest.json");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    manifest = parseManifest(await res.json());
  } catch (err) {
    console.error("manifest load failed", err);
    els.loadError.hidden = false;
    return;
  }
  buildFilters();
  startRun("alle");
}

init();
