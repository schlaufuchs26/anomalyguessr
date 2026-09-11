import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { DAILY_COUNT, dateFromKey, dateLabel, pickDaily } from "./daily";
import { type Manifest, parseManifest, type Scene } from "./manifest";
import { loadManifest, postModeration } from "./src/api";
import { buildEndData } from "./src/endView";
import { resolveGuess } from "./src/guess";
import { PhotoStage } from "./src/PhotoStage";
import type { PhotoHit, PhotoMarker } from "./src/photoInteractions";

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
  const [status, setStatus] = useState<"loading" | "error" | "playing" | "end">(
    "loading",
  );
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [moderation, setModeration] = useState(false);
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

  const scene = queue[index];
  const nextBtnRef = useRef<HTMLButtonElement>(null);
  const restartBtnRef = useRef<HTMLButtonElement>(null);

  // The vanilla game moved focus to "Next"/"Results" after every guess and to
  // "Play again" on the end screen, so keyboard players can keep going with
  // Enter; keep that behavior.
  useEffect(() => {
    if (answered) nextBtnRef.current?.focus();
  }, [answered]);
  useEffect(() => {
    if (status === "end") restartBtnRef.current?.focus();
  }, [status]);

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
    setStatus("playing");
  };
  // The load effect runs exactly once; the ref keeps it off the dependency
  // list (startRun is recreated every render but only used for that one call).
  const startRunRef = useRef(startRun);
  startRunRef.current = startRun;

  useEffect(() => {
    void (async () => {
      try {
        const { manifest: loaded, moderation: devMode } =
          await loadManifest(parseManifest);
        setManifest(loaded);
        startRunRef.current(dailySet(loaded, new Date()), devMode, new Date());
      } catch (err) {
        console.error("manifest load failed", err);
        setLoadError(true);
        setStatus("error");
      }
    })();
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
    // A correct guess dissolves into the untouched original (ticket #1147);
    // the layer is preloaded, so this only waits when it is still downloading.
    if (result.hit) {
      setRevealPending(true);
      setOriginalView(true);
    }
  };

  const onOriginalLoaded = () => {
    if (!revealPending) return;
    setRevealPending(false);
    setCorrecting(true);
    setAnnounce(
      "Correct. Now showing the original photo: the anomaly is gone.",
    );
  };

  const onOriginalError = () => {
    if (!revealPending) return;
    setRevealPending(false);
    setAnnounce("The original photo could not be loaded.");
    setOriginalView(false);
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
        <Header />
        <p className="load-error">Loading the day's scenes…</p>
        <Footer />
      </main>
    );
  }

  if (status === "error") {
    return (
      <main>
        <Header />
        <p id="load-error" className="load-error" hidden={!loadError}>
          The scenes could not be loaded. Is the page being served from a
          server?
        </p>
        <Footer />
      </main>
    );
  }

  if (status === "end") {
    const end = buildEndData(queue, scores, quizLabel());
    return (
      <main>
        <Header />
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

  return (
    <main>
      <Header />
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

function Header() {
  return (
    <header className="top">
      <h1>🦊 AnomalyGuessr</h1>
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
