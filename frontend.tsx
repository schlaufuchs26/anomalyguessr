import { type RefObject, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { DAILY_COUNT, dateFromKey, dateLabel, pickDaily } from "./daily";
import { type Manifest, parseManifest, type Scene } from "./manifest";
import { loadManifest, postModeration } from "./src/api";
import { buildEndData } from "./src/endView";
import { resolveGuess } from "./src/guess";
import { isDeliberateNavigation } from "./src/leaveGuard";
import { ModerationEmpty } from "./src/ModerationEmpty";
import { PhotoStage } from "./src/PhotoStage";
import type { PhotoHit, PhotoMarker } from "./src/photoInteractions";
import {
  appBase,
  effectiveMode,
  type Mode,
  modeFromPath,
  modePath,
} from "./src/routes";

/** The app base directory (`/` on prod, `/anomalyguessr/` on dev). */
const APP_BASE = appBase();
/** sessionStorage key the 404 shim fills before redirecting to the base. */
const REDIRECT_KEY = "anomalyguessr:redirect";

/**
 * The path the app was asked for at boot: the 404 shim's stashed target on a
 * Pages hard load of a mode path, otherwise the live location.
 */
function initialPath(): string {
  try {
    const stashed = sessionStorage.getItem(REDIRECT_KEY);
    if (stashed) {
      sessionStorage.removeItem(REDIRECT_KEY);
      return stashed;
    }
  } catch {
    // sessionStorage unavailable: use the live location
  }
  return window.location.pathname;
}

/**
 * AnomalyGuessr app (ticket #1172): the game UI migrated from the vanilla
 * game.ts DOM calls to React. The pure logic (manifest/scoring/layout/daily)
 * stays in plain TS modules; this file is the stateful view shell (photo,
 * HUD, navigation, moderation box) and owns the run state.
 */

const VERDICT_COPY: Record<
  "saved" | "warm" | "miss",
  { headline: string; note: string }
> = {
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

interface GuessView {
  verdict: "saved" | "warm" | "miss";
  headline: string;
  note: string;
  score: number;
  penalty: string;
  anomaly: string;
  spot: string;
  explanation: string;
  references: { label: string; url: string }[];
}

interface ModerationState {
  status: string;
  done: boolean;
  busy: boolean;
}

const IDLE_MODERATION: ModerationState = {
  status: "",
  done: false,
  busy: false,
};

function buildGuessView(
  scene: Scene,
  score: number,
  verdict: GuessView["verdict"],
  hintsUsed: number,
): GuessView {
  const penalty =
    hintsUsed === 0
      ? "no hints"
      : hintsUsed === 1
        ? "1 hint used"
        : `${hintsUsed} hints used`;
  return {
    verdict,
    headline: VERDICT_COPY[verdict].headline,
    note: VERDICT_COPY[verdict].note,
    score,
    penalty,
    anomaly: scene.anomaly,
    spot: scene.hints[2],
    explanation: scene.explanation ?? "",
    references: scene.references ?? [],
  };
}

/** Today's set: pipeline daily manifests ship the day's scenes in order. */
function dailySet(manifest: Manifest, day: Date): Scene[] {
  if (manifest.version === 1)
    return pickDaily(manifest.scenes, day, DAILY_COUNT);
  return manifest.scenes;
}

export function App() {
  const [status, setStatus] = useState<
    "loading" | "error" | "home" | "playing" | "end" | "empty"
  >("loading");
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [moderation, setModeration] = useState(false);
  /** The manifest's dev-instance flag; gates the gallery menu (#1204). */
  const [devMode, setDevMode] = useState(false);
  const [queue, setQueue] = useState<Scene[]>([]);
  const [index, setIndex] = useState(0);
  const [hintsUsed, setHintsUsed] = useState(0);
  const [scores, setScores] = useState<number[]>([]);
  const [answered, setAnswered] = useState(false);
  const [guess, setGuess] = useState<GuessView | null>(null);
  const [markers, setMarkers] = useState<PhotoMarker[]>([]);
  /** Compare-button state: true means the original is (or will be) showing. */
  const [originalView, setOriginalView] = useState(false);
  /** Instant compare swap (`.show`); the reveal animation uses `correcting`. */
  const [showOriginal, setShowOriginal] = useState(false);
  const [correcting, setCorrecting] = useState(false);
  const [revealPending, setRevealPending] = useState(false);
  const [announce, setAnnounce] = useState("");
  const [quizDay, setQuizDay] = useState(() => new Date());
  const [mod, setMod] = useState<ModerationState>(IDLE_MODERATION);
  const [modFeedback, setModFeedback] = useState("");
  const [loadError, setLoadError] = useState(false);
  /**
   * The mode the URL names (ticket #1223): null = frontpage, "daily" or
   * "moderation". The path is the source of truth; Back/Forward and reloads
   * land here through the `popstate` listener below.
   */
  const [route, setRoute] = useState<Mode | null>(() =>
    modeFromPath(initialPath(), APP_BASE),
  );

  const scene = queue[index];
  /**
   * True while a run is under way and at least one scene already carries a
   * score (ticket #1225). Reloading then throws away answers Evan cares
   * about; before the first answer there is nothing to lose, and the end
   * screen and the empty state have no run to protect.
   */
  const runInProgress =
    status === "playing" && scores.some((score) => score >= 0);
  /**
   * Set right before a deliberate unload, a click on one of the app's own
   * links (tickets #1225/#1230); the guard reads it and stays quiet for
   * those. `pageshow` re-arms the warning when a Back from such a page
   * restores this one from the cache.
   */
  const leavingDeliberatelyRef = useRef(false);
  const nextBtnRef = useRef<HTMLButtonElement>(null);
  const restartBtnRef = useRef<HTMLButtonElement>(null);
  /** First frontpage button: the keyboard focus target on the frontpage. */
  const firstModeRef = useRef<HTMLButtonElement>(null);
  /** The reveal layer, so the guess can check it without waiting on an event. */
  const originalImgRef = useRef<HTMLImageElement | null>(null);

  // The vanilla game moved focus to "Next"/"Results" after every guess and to
  // "Play again" on the end screen, so keyboard players can keep going with
  // Enter; keep that behavior.
  useEffect(() => {
    if (answered) nextBtnRef.current?.focus();
  }, [answered]);
  useEffect(() => {
    if (status === "end") restartBtnRef.current?.focus();
  }, [status]);
  useEffect(() => {
    if (status === "home") firstModeRef.current?.focus();
  }, [status]);

  /**
   * Reload/close guard (ticket #1225): Evan's rule is "reload the page in the
   * middle of a daily and it should warn you". `beforeunload` is the only
   * hook the browser offers, and plain in-app moves (mode switch, Play again,
   * the frontpage button, the empty state's Reload) never unload the page, so
   * they cannot reach this listener; following one of the app's own links
   * does, and is a deliberate move (see `isDeliberateNavigation`). A Back that
   * leaves the app is not a click and should warn (see the #1223 path
   * routing).
   */
  useEffect(() => {
    if (!runInProgress) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (leavingDeliberatelyRef.current) return;
      // Modern browsers trigger their own prompt when the event is canceled;
      // `returnValue` keeps the legacy path working in older ones.
      event.preventDefault();
      event.returnValue = "";
    };
    /**
     * Watch clicks in the document (ticket #1230) instead of marking every
     * link by hand: the mode and frontpage links unload the page like the
     * gallery link does, and a new one is covered without extra wiring.
     */
    const onClick = (event: MouseEvent) => {
      if (isDeliberateNavigation(event)) leavingDeliberatelyRef.current = true;
    };
    const onPageShow = () => {
      leavingDeliberatelyRef.current = false;
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    window.addEventListener("pageshow", onPageShow);
    document.addEventListener("click", onClick, true);
    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
      window.removeEventListener("pageshow", onPageShow);
      document.removeEventListener("click", onClick, true);
    };
  }, [runInProgress]);

  /** Start (or restart) a run over the given scene queue. */
  const startRun = (scenes: Scene[], on: boolean, day: Date) => {
    setModeration(on);
    setQueue(scenes);
    setIndex(0);
    setHintsUsed(0);
    setScores([]);
    setAnswered(false);
    setGuess(null);
    setMarkers([]);
    setOriginalView(false);
    setShowOriginal(false);
    setCorrecting(false);
    setRevealPending(false);
    setAnnounce("");
    setQuizDay(day);
    setMod(IDLE_MODERATION);
    setModFeedback("");
    // An empty queue still enters a run state: the moderation mode shows the
    // pipeline buffer + Generate button there (#1202/#1210).
    setStatus(scenes.length === 0 ? "empty" : "playing");
  };
  /** Start the run of a frontpage mode over the loaded manifest (#1214). */
  const startMode = (mode: Mode, loaded: Manifest) => {
    const now = new Date();
    startRun(
      mode === "daily" ? dailySet(loaded, now) : loaded.scenes,
      mode === "moderation",
      now,
    );
  };
  const startModeRef = useRef(startMode);
  startModeRef.current = startMode;

  /**
   * Frontpage selection: Deep-linkable (ticket #1223), so Daily plays the
   * day's set and Moderation the queue under their own path. The URL is the
   * source of truth; a `pushState` adds the history entry (Back returns to
   * the frontpage) and setting `route` too keeps the click instant.
   */
  const chooseMode = (mode: Mode) => {
    const path = modePath(APP_BASE, mode);
    if (window.location.pathname !== path) {
      window.history.pushState(null, "", path);
    }
    setRoute(mode);
  };

  /** Back/Forward move between the history entries the app pushed. */
  useEffect(() => {
    const onPop = () =>
      setRoute(modeFromPath(window.location.pathname, APP_BASE));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  /**
   * Apply the route once the manifest is in hand (ticket #1223): start the
   * named mode, or show the frontpage. The URL is rewritten (replaceState, no
   * new entry) to match what actually runs, so a `/moderation` load on a host
   * whose manifest has no dev flag, or an unknown path, lands on the bare
   * frontpage instead of leaving a dead URL.
   */
  useEffect(() => {
    if (!manifest) return;
    const mode = effectiveMode(route, devMode);
    const path = modePath(APP_BASE, mode);
    if (window.location.pathname !== path) {
      window.history.replaceState(null, "", path);
    }
    if (mode) {
      startModeRef.current(mode, manifest);
      return;
    }
    if (route !== null) setRoute(null);
    setStatus("home");
  }, [route, manifest, devMode]);

  /**
   * Fetch the manifest and set the dev flag. The route effect above then
   * starts the URL's mode or shows the frontpage (ticket #1223); this is also
   * the Reload action of the empty state (#1202/#1210), which re-applies the
   * current route. The ref keeps the mount effect off the dependency list.
   */
  const load = async () => {
    setStatus("loading");
    try {
      const { manifest: loaded, moderation: dev } =
        await loadManifest(parseManifest);
      setManifest(loaded);
      setDevMode(dev);
    } catch (err) {
      console.error("manifest load failed", err);
      setLoadError(true);
      setStatus("error");
    }
  };
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    void loadRef.current();
  }, []);

  /** Reset every piece of per-scene state (guess, markers, reveal, mod box). */
  const resetSceneState = () => {
    setHintsUsed(0);
    setAnswered(false);
    setGuess(null);
    setMarkers([]);
    setOriginalView(false);
    setShowOriginal(false);
    setCorrecting(false);
    setRevealPending(false);
    setAnnounce("");
    setMod(IDLE_MODERATION);
    setModFeedback("");
  };

  /**
   * The #1147 dissolve: one CSS pass over the layer (`.reveal`) plus the
   * glitch shimmer on the photo (`.correcting`). Both the already-loaded and
   * the load-landed path share it.
   */
  const startOriginalReveal = () => {
    setRevealPending(false);
    setCorrecting(true);
    setAnnounce(
      "Correct. Now showing the original photo: the anomaly is gone.",
    );
  };

  /** The original cannot be shown: say so instead of revealing nothing. */
  const failOriginalReveal = () => {
    setRevealPending(false);
    setAnnounce("The original photo could not be loaded.");
    setOriginalView(false);
  };

  const onGuess = (hit: PhotoHit) => {
    if (!scene || answered) return;
    const result = resolveGuess(scene, hit.x, hit.y, hintsUsed);
    const nextScores = [...scores];
    nextScores[index] = result.score;
    setScores(nextScores);
    setAnswered(true);
    setGuess(buildGuessView(scene, result.score, result.verdict, hintsUsed));
    setMarkers([
      { type: "click", x: result.x, y: result.y },
      {
        type: "answer",
        x: scene.answer.x,
        y: scene.answer.y,
        r: scene.answer.r,
      },
    ]);
    // A correct guess dissolves into the untouched original (ticket #1147).
    // The layer is preloaded per scene, so its load event already fired while
    // the scene was on screen and no second one arrives: run the animation
    // now when the bytes are there. A completed image without dimensions is a
    // failed (or empty) source that will never fire anything, so say so
    // instead of waiting forever.
    if (result.hit) {
      setOriginalView(true);
      const img = originalImgRef.current;
      if (img?.complete && img.naturalWidth > 0) {
        startOriginalReveal();
      } else if (img?.complete) {
        failOriginalReveal();
      } else {
        setRevealPending(true);
      }
    }
  };

  const onOriginalLoaded = () => {
    if (!revealPending) return;
    startOriginalReveal();
  };

  const onOriginalError = () => {
    if (!revealPending) return;
    failOriginalReveal();
  };

  /** Manual compare toggle: instant swap, no animation. */
  const toggleCompare = () => {
    setRevealPending(false);
    setCorrecting(false);
    const next = !originalView;
    setOriginalView(next);
    setShowOriginal(next);
  };

  /** Double-click zooms in, so undo the guess the first click already made. */
  const undoGuess = () => {
    if (!answered) return;
    const nextScores = [...scores];
    nextScores[index] = -1;
    setScores(nextScores);
    resetSceneState();
  };

  const onHint = () => {
    if (!scene || answered || hintsUsed >= 3) return;
    setHintsUsed(hintsUsed + 1);
  };

  const next = () => {
    if (index >= queue.length - 1) {
      setStatus("end");
      return;
    }
    setIndex(index + 1);
    resetSceneState();
  };

  const restart = () => {
    startRun(queue, moderation, new Date());
  };

  const quizLabel = (): string => {
    const day = quizDay;
    if (manifest?.version === 2 && manifest.date)
      return dateLabel(dateFromKey(manifest.date));
    return dateLabel(day);
  };

  const hintLabel = (): string => {
    if (hintsUsed >= 3) return "💡 No hints left";
    if (hintsUsed === 0) return "💡 Show hint";
    return `💡 Show hint (${hintsUsed}/3)`;
  };

  const postModerationAction = async (action: "accept" | "reject") => {
    if (!scene || mod.busy || mod.done) return;
    setMod({ ...mod, busy: true, status: "Saving…" });
    try {
      await postModeration(scene.id, action, modFeedback.trim());
      setMod({
        status: action === "accept" ? "✓ Accepted." : "✕ Rejected.",
        done: true,
        busy: false,
      });
    } catch (err) {
      setMod({
        status: `Save failed: ${String(err)}`,
        done: false,
        busy: false,
      });
    }
  };

  if (status === "loading") {
    return (
      <main>
        <Header showMenu={devMode} />
        <p className="load-error">Loading the day's scenes…</p>
        <Footer />
      </main>
    );
  }

  if (status === "error") {
    return (
      <main>
        <Header showMenu={devMode} />
        <p id="load-error" className="load-error" hidden={!loadError}>
          The scenes could not be loaded. Is the page being served from a
          server?
        </p>
        <Footer />
      </main>
    );
  }

  if (status === "home") {
    return (
      <main>
        <Header showMenu={devMode} />
        <ModeSelect
          devMode={devMode}
          moderationCount={manifest?.scenes.length ?? 0}
          onSelect={chooseMode}
          firstRef={firstModeRef}
        />
        <Footer />
      </main>
    );
  }

  if (status === "end") {
    const end = buildEndData(queue, scores, quizLabel());
    return (
      <main>
        <Header showMenu={devMode} />
        <section id="end" className="end">
          <h2>Mission complete</h2>
          <p id="end-date" className="end-date">
            Daily quiz · {end.dateLabel}
          </p>
          <ol id="end-list" className="end-list">
            {end.rows.map((row) => (
              <li key={row.index}>
                {row.index}. {row.title} · {row.score} pts
              </li>
            ))}
          </ol>
          <p id="end-text">{end.text}</p>
          <button
            id="restart-btn"
            ref={restartBtnRef}
            className="btn primary"
            type="button"
            onClick={restart}
          >
            Play again
          </button>
        </section>
        <Footer />
      </main>
    );
  }

  if (status === "empty") {
    return (
      <main>
        <Header showMenu={devMode} />
        {moderation ? (
          <ModerationEmpty onReload={() => void loadRef.current()} />
        ) : (
          <section id="empty" className="empty-state">
            <h2>No scenes available</h2>
            <p>New scenes appear after the next daily run.</p>
            <button
              id="reload-btn"
              className="btn primary"
              type="button"
              onClick={() => void loadRef.current()}
            >
              Reload
            </button>
          </section>
        )}
        <Footer />
      </main>
    );
  }

  return (
    <main>
      <Header showMenu={devMode} />
      <section id="game" className="game">
        <div className="meta">
          <span id="scene-title" className="scene-title">
            {scene?.title}
          </span>
          <span id="scene-place" className="scene-place">
            {scene ? `${scene.place}, ${scene.year}` : ""}
          </span>
          <span id="scene-progress" className="progress">
            {`${index + 1} / ${queue.length}`}
          </span>
        </div>
        <p id="scene-desc" className="scene-desc">
          {scene?.description}
        </p>
        <PhotoStage
          key={`${scene?.id ?? ""}-${index}`}
          alt={
            scene
              ? `Historical photo: ${scene.title} (${scene.place}, ${scene.year})`
              : ""
          }
          image={scene?.image ?? ""}
          original={scene?.original ?? ""}
          originalRef={originalImgRef}
          answered={answered}
          showOriginal={showOriginal}
          correcting={correcting}
          markers={markers}
          onGuess={onGuess}
          onUndoGuess={undoGuess}
          onOriginalLoaded={onOriginalLoaded}
          onOriginalError={onOriginalError}
        />
        <p id="img-announce" className="sr-only" aria-live="polite">
          {announce}
        </p>
        <div className="hud">
          <div id="hint-box" className="hint-box" aria-live="polite">
            <button
              id="hint-btn"
              className="btn"
              type="button"
              disabled={hintsUsed >= 3}
              onClick={onHint}
            >
              {hintLabel()}
            </button>
            <p id="hint-text" className="hint-text">
              {hintsUsed > 0 && scene
                ? `Hint ${hintsUsed}/3: ${scene.hints[hintsUsed - 1]}`
                : ""}
            </p>
          </div>
          <div
            id="result"
            className={`result${guess ? ` ${guess.verdict}` : ""}`}
            aria-live="polite"
          >
            {guess ? (
              <>
                <p className="headline">{guess.headline}</p>
                <p className="score">
                  <strong>{guess.score} points</strong> · {guess.penalty}
                </p>
                <p>{guess.note}</p>
                <p>
                  🔍 The anomaly was: <strong>{guess.anomaly}</strong>. Exact
                  spot: {guess.spot}
                </p>
              </>
            ) : null}
          </div>
          {guess?.explanation ? (
            <div id="why" className="why">
              <p className="why-head">Why?</p>
              <p className="why-text">{guess.explanation}</p>
              {guess.references.length > 0 ? (
                <ul className="why-refs">
                  {guess.references.map((ref) => (
                    <li key={ref.url}>
                      <a
                        href={ref.url}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {ref.label}
                      </a>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}
          <div className="nav">
            <button
              id="compare-btn"
              className="btn ghost"
              type="button"
              hidden={!answered}
              onClick={toggleCompare}
            >
              {originalView ? "🖼️ Edited image" : "📷 Show original"}
            </button>
            <button
              id="next-btn"
              ref={nextBtnRef}
              className="btn primary"
              type="button"
              hidden={!answered}
              onClick={next}
            >
              {index >= queue.length - 1 ? "Results →" : "Next →"}
            </button>
          </div>
          {moderation && answered ? (
            <div id="moderate" className="moderate">
              <label className="moderate-label" htmlFor="moderate-feedback">
                Moderation verdict
              </label>
              <input
                id="moderate-feedback"
                className="moderate-feedback"
                type="text"
                placeholder="Feedback for the pipeline (optional)…"
                maxLength={2000}
                value={modFeedback}
                onChange={(e) => setModFeedback(e.target.value)}
              />
              <div className="moderate-actions">
                <button
                  id="reject-btn"
                  className="btn ghost"
                  type="button"
                  disabled={mod.busy || mod.done}
                  onClick={() => void postModerationAction("reject")}
                >
                  ✕ Reject
                </button>
                <button
                  id="accept-btn"
                  className="btn primary"
                  type="button"
                  disabled={mod.busy || mod.done}
                  onClick={() => void postModerationAction("accept")}
                >
                  ✓ Accept
                </button>
              </div>
              <p
                id="moderate-status"
                className="moderate-status"
                aria-live="polite"
              >
                {mod.status}
              </p>
            </div>
          ) : null}
        </div>
      </section>
      <p id="load-error" className="load-error" hidden={!loadError}>
        The scenes could not be loaded. Is the page being served from a server?
      </p>
      <Footer />
    </main>
  );
}

/**
 * The image-overview page (ticket #1204), a sibling of the game under the same
 * /anomalyguessr/ mount. Rendered only on the dev instance: the static prod
 * build (GitHub Pages) has no gallery, and a dead link there would 404.
 */
const GALLERY_URL = "gallery/";

/**
 * The frontpage (ticket #1214): the game modes as big buttons, shown before
 * any run. Daily is always there; Moderation only on the dev instance, whose
 * queue API flags its manifest with `moderation: true`. The moderation count
 * comes straight from that manifest (the unmoderated queue it plays), so the
 * frontend never re-derives an order or a set. Each button publishes its mode
 * to the URL (ticket #1223); the frontpage itself is the bare base URL.
 */
function ModeSelect({
  devMode,
  moderationCount,
  onSelect,
  firstRef,
}: {
  devMode: boolean;
  moderationCount: number;
  onSelect: (mode: Mode) => void;
  firstRef: RefObject<HTMLButtonElement | null>;
}) {
  return (
    <section id="home" className="home">
      <h2 className="home-title">Choose a mode</h2>
      <div className="modes">
        <button
          id="mode-daily"
          ref={firstRef}
          className="mode-btn"
          type="button"
          data-testid="mode-daily"
          onClick={() => onSelect("daily")}
        >
          <span className="mode-name">Daily</span>
          <span className="mode-sub">Today's set</span>
        </button>
        {devMode ? (
          <button
            id="mode-moderation"
            className="mode-btn"
            type="button"
            data-testid="mode-moderation"
            onClick={() => onSelect("moderation")}
          >
            <span className="mode-name">Moderation</span>
            <span className="mode-sub">
              {moderationCount > 0
                ? `${moderationCount} ${moderationCount === 1 ? "scene" : "scenes"} waiting for a verdict`
                : "Nothing to review right now"}
            </span>
          </button>
        ) : null}
      </div>
    </section>
  );
}

/**
 * The page header. The gallery link (dev only) is an ordinary anchor: the
 * reload guard recognizes a click on it as a deliberate leave (ticket
 * #1230), so no handler is needed here.
 */
function Header({ showMenu }: { showMenu: boolean }) {
  return (
    <header className="top">
      <div className="top-row">
        <h1>🦊 AnomalyGuessr</h1>
        {showMenu ? (
          <a
            className="menu-btn"
            href={GALLERY_URL}
            title="Overview of all images"
            data-testid="gallery-menu"
          >
            ☰ Gallery
          </a>
        ) : null}
      </div>
      <p className="tagline">
        Time-travel protection squad. An anomaly has been reported in a
        historical photo: something that could not possibly be there, a modern
        object, a time traveler or a robot from a fictional future. Click the
        spot where something doesn't belong.
      </p>
    </header>
  );
}

function Footer() {
  return (
    <footer className="foot">
      <p>
        Historical photos: public-domain images (Public Domain / CC0) via
        Wikimedia Commons. The anomalies were added with AI image editing.
      </p>
    </footer>
  );
}

const el = document.getElementById("root");
if (el) {
  createRoot(el).render(<App />);
}
