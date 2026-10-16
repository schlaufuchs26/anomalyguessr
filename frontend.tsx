import { type RefObject, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { DAILY_COUNT, dateFromKey, dateLabel, pickDaily } from "./daily";
import { type Manifest, parseManifest, type Scene } from "./manifest";
import { REJECT_REASONS, type RejectReason } from "./rejectReasons";
import { canRequestRepair, isRepairableReason } from "./repairReasons";
import { bearingTo, MISS_PENALTY, runningTotal } from "./scoring";
import {
  loadDailyManifest,
  loadLiveManifest,
  loadManifest,
  loadRemoderateManifest,
  type ModerationTag,
  postModeration,
  postRating,
  postTag,
} from "./src/api";
import { buildEndData } from "./src/endView";
import { GenerateControl } from "./src/GenerateControl";
import { resolveGuess } from "./src/guess";
import { isDeliberateNavigation } from "./src/leaveGuard";
import { ModerationEmpty } from "./src/ModerationEmpty";
import { PhotoStage } from "./src/PhotoStage";
import type { MissCue, PhotoHit, PhotoMarker } from "./src/photoInteractions";
import { RatingControl } from "./src/RatingControl";
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
/** localStorage key the Remoderate mode's position survives a reload (#2606). */
const REMODERATE_INDEX_KEY = "anomalyguessr:remoderate:index";

/**
 * The saved Remoderate position (ticket #2606), clamped to a valid index for
 * a queue of `length` scenes. Returns 0 when nothing is saved, the value is
 * unreadable, or the saved position is past a now-shorter queue.
 */
function readRemoderateIndex(length: number): number {
  if (length <= 0) return 0;
  try {
    const raw = localStorage.getItem(REMODERATE_INDEX_KEY);
    const n = raw === null ? 0 : Number.parseInt(raw, 10);
    if (!Number.isFinite(n) || n < 0) return 0;
    return Math.min(n, length - 1);
  } catch {
    return 0;
  }
}

/** Remember the Remoderate position so a reload continues (#2606). */
function saveRemoderateIndex(index: number): void {
  try {
    localStorage.setItem(REMODERATE_INDEX_KEY, String(index));
  } catch {
    // localStorage unavailable: the run still works, it just restarts
  }
}

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

/** The "funny" tag's box state: posting, set, or a failed post. */
interface TagUi {
  busy: boolean;
  tagged: boolean;
  error: string;
}

/** The tag box state keyed by tag; "funny" is the only plain tag left (#2605). */
type TagUiState = Record<ModerationTag, TagUi>;

const IDLE_TAG_STATE: TagUiState = {
  funny: { busy: false, tagged: false, error: "" },
};

/** The one tag in display order, with its checkbox label. */
const TAGS: { tag: ModerationTag; label: string }[] = [
  { tag: "funny", label: "😄 Funny" },
];

/** The 0-10 quality rating's box state (#2605): posting, value, failed post. */
interface RatingUi {
  busy: boolean;
  value: number | null;
  error: string;
}

const IDLE_RATING: RatingUi = { busy: false, value: null, error: "" };

function buildGuessView(
  scene: Scene,
  score: number,
  verdict: GuessView["verdict"],
  misses: number,
): GuessView {
  const penalty =
    misses === 0 ? "no misses" : misses === 1 ? "1 miss" : `${misses} misses`;
  return {
    verdict,
    headline: VERDICT_COPY[verdict].headline,
    note: VERDICT_COPY[verdict].note,
    score,
    penalty,
    anomaly: scene.anomaly,
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

/** "place, year", with a source that carries no place showing only the year. */
export function scenePlaceLine(scene: Scene): string {
  return [scene.place, scene.year].filter(Boolean).join(", ");
}

export function App() {
  const [status, setStatus] = useState<
    "loading" | "error" | "home" | "playing" | "end" | "empty"
  >("loading");
  const [manifest, setManifest] = useState<Manifest | null>(null);
  /**
   * The set the Daily mode plays. On the dev instance the manifest is the
   * unmoderated queue, so Daily fetches the day's set separately
   * (`scenes/daily.json`, ticket #1221); on prod the manifest already is the
   * day's set and both states hold it.
   */
  const [dailyManifest, setDailyManifest] = useState<Manifest | null>(null);
  /**
   * The set prod is serving right now (`scenes/live.json`, ticket #1237),
   * fetched on the dev instance for the Live mode; null on prod and whenever
   * the fetch failed, which is what hides the Live mode.
   */
  const [liveManifest, setLiveManifest] = useState<Manifest | null>(null);
  /**
   * The already-decided scenes (`scenes/remoderate.json`, ticket #2606),
   * fetched on the dev instance for the Remoderate mode; null on prod and
   * whenever the fetch failed, which is what hides the mode.
   */
  const [remoderateManifest, setRemoderateManifest] = useState<Manifest | null>(
    null,
  );
  const [moderation, setModeration] = useState(false);
  /** The Remoderate mode (#2606): same box, decided scenes, oldest first. */
  const [remoderate, setRemoderate] = useState(false);
  /** The manifest's dev-instance flag; gates the gallery menu (#1204). */
  const [devMode, setDevMode] = useState(false);
  const [queue, setQueue] = useState<Scene[]>([]);
  const [index, setIndex] = useState(0);
  const [misses, setMisses] = useState(0);
  /**
   * Nonce for the HUD score counter's dip animation (#1490): bumped on every
   * miss so the value remounts and flashes once, then holds. It is not the
   * score itself; the number comes from `runningTotal`.
   */
  const [scoreDips, setScoreDips] = useState(0);
  const [cue, setCue] = useState<MissCue | null>(null);
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
  /**
   * The rejection reasons ticked in the moderation box (ticket #2585):
   * several can be selected before one Reject submit, and all of them travel
   * with the decision.
   */
  const [modReasons, setModReasons] = useState<RejectReason[]>([]);
  /**
   * The "funny" moderation tag (#1502): a separate, one-way checkbox next to
   * accept/reject. The "great" tick became the 0-10 rating below (#2605).
   */
  const [tags, setTags] = useState<TagUiState>(IDLE_TAG_STATE);
  /** The 0-10 quality rating (#2605): independent of the verdict. */
  const [rating, setRating] = useState<RatingUi>(IDLE_RATING);
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
  /** First frontpage mode link: the keyboard focus target on the frontpage. */
  const firstModeRef = useRef<HTMLAnchorElement>(null);
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
  // Remember where the Remoderate run is, so a reload continues instead of
  // starting over (ticket #2606).
  useEffect(() => {
    if (remoderate) saveRemoderateIndex(index);
  }, [remoderate, index]);

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
  const startRun = (
    scenes: Scene[],
    on: boolean,
    remod: boolean,
    day: Date,
  ) => {
    setModeration(on);
    setRemoderate(remod);
    setQueue(scenes);
    // Remoderate resumes where the last run left off (ticket #2606); every
    // other mode starts at the first scene.
    setIndex(remod ? readRemoderateIndex(scenes.length) : 0);
    setMisses(0);
    setCue(null);
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
    setModReasons([]);
    setTags(IDLE_TAG_STATE);
    setRating(IDLE_RATING);
    // An empty queue still enters a run state: the moderation mode shows the
    // pipeline buffer + Generate button there (#1202/#1210).
    setStatus(scenes.length === 0 ? "empty" : "playing");
  };
  /** Start the run of a frontpage mode over the loaded manifest (#1214). */
  const startMode = (mode: Mode, loaded: Manifest) => {
    const now = new Date();
    // Moderation and Remoderate review the raw queue; Daily and Live play a
    // daily set (prepared v2 scenes in order, or 5 seeded from a v1 pool).
    // Both review modes are dev-only, so the production bundle drops the
    // comparison (#1374).
    const reviewing =
      process.env.NODE_ENV !== "production" &&
      (mode === "moderation" || mode === "remoderate");
    startRun(
      reviewing ? loaded.scenes : dailySet(loaded, now),
      reviewing,
      mode === "remoderate",
      now,
    );
  };
  const startModeRef = useRef(startMode);
  startModeRef.current = startMode;

  /** Back/Forward move between the history entries the browser keeps. */
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
    if (!manifest || !dailyManifest) return;
    const mode = effectiveMode(
      route,
      devMode,
      liveManifest !== null,
      remoderateManifest !== null,
    );
    // Daily previews the next set to ship (#1221); Live replays the set prod
    // serves now (#1237); Moderation plays the raw queue and Remoderate the
    // already-decided scenes (#2606). Live's and Remoderate's manifests are
    // null when their fetch failed, which already degraded the mode above.
    const source =
      process.env.NODE_ENV !== "production" && mode === "live"
        ? liveManifest
        : process.env.NODE_ENV !== "production" && mode === "remoderate"
          ? remoderateManifest
          : process.env.NODE_ENV !== "production" && mode === "moderation"
            ? manifest
            : dailyManifest;
    if (mode && source) {
      const path = modePath(APP_BASE, mode);
      if (window.location.pathname !== path) {
        window.history.replaceState(null, "", path);
      }
      startModeRef.current(mode, source);
      return;
    }
    // No runnable mode (frontpage, unknown path, or an unavailable mode whose
    // URL must not stick): land on the bare frontpage.
    const home = modePath(APP_BASE, null);
    if (window.location.pathname !== home) {
      window.history.replaceState(null, "", home);
    }
    if (route !== null) setRoute(null);
    setStatus("home");
  }, [
    route,
    manifest,
    dailyManifest,
    liveManifest,
    remoderateManifest,
    devMode,
  ]);

  /**
   * Fetch the manifest and set the dev flag; on the dev instance also fetch
   * the day's set the Daily mode plays (ticket #1221). The route effect above
   * then starts the URL's mode or shows the frontpage (ticket #1223); this is
   * also the Reload action of the empty state (#1202/#1210), which re-applies
   * the current route. The ref keeps the mount effect off the dependency list.
   *
   * `showLoading` is false for a reload: the screen already has something to
   * show (the empty state), and blanking it to "Loading…" while the fetch is
   * in flight only makes it flash; the route effect swaps it once the answer
   * lands (#1287).
   */
  const load = async (showLoading = true) => {
    if (showLoading) setStatus("loading");
    try {
      const { manifest: loaded, moderation: dev } =
        await loadManifest(parseManifest);
      // The dev instance's manifest is the unmoderated queue; fetch the day's
      // set for the Daily mode too (ticket #1221). Prod's manifest already is
      // the day's set, so it doubles as the daily manifest there. A fetch
      // that fails (e.g. a server without the daily scope) must not take the
      // instance down: Daily then falls back to the queue, which is what it
      // played before #1221.
      let daily = loaded;
      let live: Manifest | null = null;
      let remoderate: Manifest | null = null;
      if (dev && process.env.NODE_ENV !== "production") {
        // The dev instance's manifest is the unmoderated queue; fetch the
        // day's set for the Daily mode (ticket #1221) and the set prod serves
        // now for the Live mode (ticket #1237). Prod's manifest already is
        // the day's set, so it doubles as the daily manifest there, and prod
        // has no Live mode. A fetch that fails (e.g. a server without the
        // scope) must not take the instance down: Daily then falls back to
        // the queue, which is what it played before #1221, and Live is simply
        // not offered.
        try {
          daily = await loadDailyManifest(parseManifest);
        } catch (err) {
          console.warn("daily manifest load failed; playing the queue", err);
        }
        try {
          live = await loadLiveManifest(parseManifest);
        } catch (err) {
          console.warn("live manifest load failed; no Live mode", err);
        }
        try {
          remoderate = await loadRemoderateManifest(parseManifest);
        } catch (err) {
          console.warn(
            "remoderate manifest load failed; no Remoderate mode",
            err,
          );
        }
      }
      setManifest(loaded);
      setDailyManifest(daily);
      setLiveManifest(live);
      setRemoderateManifest(remoderate);
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

  /**
   * Merge the scenes the queue does not carry yet after an on-demand run
   * finished (ticket #1584). The order and the index of the queue Evan is
   * working through stay untouched; new scenes only append, so triggering a
   * generation mid-run cannot drop or reorder what is already in review.
   */
  const mergeQueue = async () => {
    try {
      const { manifest: loaded } = await loadManifest(parseManifest);
      setQueue((current) => {
        const seen = new Set(current.map((s) => s.id));
        const fresh = loaded.scenes.filter((s) => !seen.has(s.id));
        return fresh.length ? [...current, ...fresh] : current;
      });
    } catch (err) {
      console.error("queue refresh failed", err);
    }
  };

  /** Reset every piece of per-scene state (guess, markers, reveal, mod box). */
  const resetSceneState = () => {
    setMisses(0);
    setCue(null);
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
    setModReasons([]);
    setTags(IDLE_TAG_STATE);
    setRating(IDLE_RATING);
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
    const result = resolveGuess(scene, hit.x, hit.y, misses);
    // A miss keeps the scene open (ticket #1407): the player pays the flat
    // penalty and gets a bearing cue, then tries again. The answer stays
    // hidden until a hit.
    if (!result.hit) {
      const next = misses + 1;
      setMisses(next);
      setScoreDips((d) => d + 1);
      setCue({
        x: hit.x,
        y: hit.y,
        angle: bearingTo({ x: hit.x, y: hit.y }, scene.answer),
        n: next,
      });
      setAnnounce(`Miss. −${MISS_PENALTY} points.`);
      return;
    }
    setCue(null);
    const nextScores = [...scores];
    nextScores[index] = result.score;
    setScores(nextScores);
    setAnswered(true);
    setGuess(buildGuessView(scene, result.score, result.verdict, misses));
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
    setOriginalView(true);
    const img = originalImgRef.current;
    if (img?.complete && img.naturalWidth > 0) {
      startOriginalReveal();
    } else if (img?.complete) {
      failOriginalReveal();
    } else {
      setRevealPending(true);
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

  const next = () => {
    if (index >= queue.length - 1) {
      setStatus("end");
      return;
    }
    setIndex(index + 1);
    resetSceneState();
  };

  const restart = () => {
    startRun(queue, moderation, remoderate, new Date());
  };

  const quizLabel = (): string => {
    const day = quizDay;
    if (manifest?.version === 2 && manifest.date)
      return dateLabel(dateFromKey(manifest.date));
    return dateLabel(day);
  };

  /**
   * Tick or untick one rejection reason (ticket #2585): the moderation box
   * collects several before the single Reject submit.
   */
  const toggleModReason = (reason: RejectReason) => {
    setModReasons((prev) =>
      prev.includes(reason)
        ? prev.filter((r) => r !== reason)
        : [...prev, reason],
    );
  };

  /**
   * The moderation verdict (dev instance only, ticket #1163), compiled out of
   * the production bundle (#1374) together with the queue API prefix it calls.
   * A landed verdict advances to the next scene (ticket #1888); only the last
   * scene stays, so its status is readable and Next still ends the run.
   * A reject carries every ticked reason (ticket #2585); a "Needs work"
   * request carries them too, but sends the scene to the repair pass instead
   * of rejecting it (ticket #2590).
   */
  const postModerationAction =
    process.env.NODE_ENV !== "production"
      ? async (action: "accept" | "reject" | "needs_work") => {
          if (!scene || mod.busy || mod.done) return;
          const reasons = action === "accept" ? [] : modReasons;
          setMod({ ...mod, busy: true, status: "Saving…" });
          try {
            await postModeration(scene.id, action, modFeedback.trim(), reasons);
            setMod({
              status:
                action === "accept"
                  ? "✓ Accepted."
                  : action === "needs_work"
                    ? `🛠 Needs work · ${reasons.join(", ")}`
                    : reasons.length > 0
                      ? `✕ Rejected · ${reasons.join(", ")}`
                      : "✕ Rejected.",
              done: true,
              busy: false,
            });
            // A queue tool moves on by itself (ticket #1888). The last scene
            // stays put so its status is readable and Next can end the run.
            if (index < queue.length - 1) next();
          } catch (err) {
            setMod({
              status: `Save failed: ${String(err)}`,
              done: false,
              busy: false,
            });
          }
        }
      : null;

  /**
   * A moderation tag click (#1502, #1541): one-way (a set tag stays set for
   * the scene), independent of the verdict and of the other tag. Compiled
   * out of the production bundle like the moderation action above.
   */
  const postTagAction =
    process.env.NODE_ENV !== "production"
      ? async (tag: ModerationTag) => {
          if (!scene || tags[tag].busy || tags[tag].tagged) return;
          setTags((prev) => ({
            ...prev,
            [tag]: { busy: true, tagged: false, error: "" },
          }));
          try {
            await postTag(scene.id, tag, true);
            setTags((prev) => ({
              ...prev,
              [tag]: { busy: false, tagged: true, error: "" },
            }));
          } catch (err) {
            setTags((prev) => ({
              ...prev,
              [tag]: { busy: false, tagged: false, error: String(err) },
            }));
          }
        }
      : null;

  /**
   * A rating click (#2605): posts the 0-10 quality value (or clears it),
   * independent of the verdict and the "funny" tag. Compiled out of the
   * production bundle like the moderation action above.
   */
  const postRatingAction =
    process.env.NODE_ENV !== "production"
      ? async (value: number | null) => {
          if (!scene || rating.busy) return;
          setRating((prev) => ({ ...prev, busy: true, error: "" }));
          try {
            const applied = await postRating(scene.id, value);
            setRating({ busy: false, value: applied, error: "" });
          } catch (err) {
            setRating((prev) => ({ ...prev, busy: false, error: String(err) }));
          }
        }
      : null;

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
          liveAvailable={liveManifest !== null}
          moderationCount={manifest?.scenes.length ?? 0}
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
        {moderation && process.env.NODE_ENV !== "production" ? (
          <ModerationEmpty onReload={() => void loadRef.current(false)} />
        ) : (
          <section id="empty" className="empty-state">
            <h2>No scenes available</h2>
            <p>New scenes appear after the next daily run.</p>
            <button
              id="reload-btn"
              className="btn primary"
              type="button"
              onClick={() => void loadRef.current(false)}
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
            {scene ? scenePlaceLine(scene) : ""}
          </span>
          <span
            id="scene-progress"
            className="progress"
            data-testid="scene-progress"
          >
            {remoderate
              ? `${index + 1} of ${queue.length}`
              : `${index + 1} / ${queue.length}`}
          </span>
          {moderation && process.env.NODE_ENV !== "production" ? (
            <a
              className="mode-switch"
              href={modePath(
                APP_BASE,
                remoderate ? "moderation" : "remoderate",
              )}
              title={
                remoderate
                  ? "Back to the unmoderated queue"
                  : "Revisit scenes that already have a verdict"
              }
              data-testid="mode-switch"
            >
              {remoderate ? "← Moderation" : "↻ Remoderate"}
            </a>
          ) : null}
          {moderation && process.env.NODE_ENV !== "production" ? (
            <GenerateControl
              onFinished={() => void mergeQueue()}
              collapsibleTrace
            />
          ) : null}
        </div>
        <p id="scene-desc" className="scene-desc">
          {scene?.description}
        </p>
        <PhotoStage
          key={`${scene?.id ?? ""}-${index}`}
          alt={
            scene
              ? `Historical photo: ${scene.title} (${scenePlaceLine(scene)})`
              : ""
          }
          image={scene?.image ?? ""}
          original={scene?.original ?? ""}
          originalRef={originalImgRef}
          answered={answered}
          showOriginal={showOriginal}
          correcting={correcting}
          markers={markers}
          cue={cue}
          onGuess={onGuess}
          onUndoGuess={undoGuess}
          onOriginalLoaded={onOriginalLoaded}
          onOriginalError={onOriginalError}
        />
        <p id="img-announce" className="sr-only" aria-live="polite">
          {announce}
        </p>
        <div className="hud">
          <div
            id="score-counter"
            className={`hud-score${scoreDips > 0 ? " dip" : ""}`}
          >
            <span className="hud-score-label">Score</span>
            <strong key={scoreDips} className="hud-score-value">
              {runningTotal(scores, answered ? 0 : misses)}
            </strong>
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
                  🔍 The anomaly was: <strong>{guess.anomaly}</strong>. Its spot
                  is circled on the photo.
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
              hidden={!answered && !remoderate}
              onClick={next}
            >
              {index >= queue.length - 1 ? "Results →" : "Next →"}
            </button>
          </div>
          {moderation &&
          (answered || remoderate) &&
          process.env.NODE_ENV !== "production" ? (
            <div id="moderate" className="moderate">
              <span className="moderate-label">Moderation verdict</span>
              {scene?.repair ? (
                <p
                  className="moderate-repair"
                  data-testid="moderate-repair"
                  title={scene.repair.detail ?? ""}
                >
                  {scene.repair.status === "repaired"
                    ? `🛠 Repaired — was: ${scene.repair.reasons.join(", ")}`
                    : `🚫 Not repairable — ${scene.repair.reasons.join(", ")}`}
                </p>
              ) : null}
              <div className="moderate-verdict">
                <button
                  id="accept-btn"
                  className="btn primary"
                  type="button"
                  disabled={mod.busy || mod.done}
                  onClick={() => void postModerationAction?.("accept")}
                >
                  ✓ Accept
                </button>
                <button
                  id="reject-btn"
                  className="btn ghost"
                  type="button"
                  disabled={mod.busy || mod.done}
                  onClick={() => void postModerationAction?.("reject")}
                >
                  ✕ Reject
                </button>
                <button
                  id="needs-work-btn"
                  className="btn ghost"
                  type="button"
                  disabled={
                    mod.busy || mod.done || !canRequestRepair(modReasons)
                  }
                  title={
                    modReasons.length === 0
                      ? "Tick at least one reason the repair should fix"
                      : canRequestRepair(modReasons)
                        ? "Send the scene back for repair with the ticked reasons (ticket #2590)"
                        : `The repair pass only fixes the click area or the text; it would return this rejected: ${modReasons
                            .filter((r) => !isRepairableReason(r))
                            .join(", ")}`
                  }
                  onClick={() => void postModerationAction?.("needs_work")}
                >
                  🛠 Needs work
                </button>
                <div className="moderate-tags">
                  {TAGS.map(({ tag, label }) => (
                    <label
                      key={tag}
                      className={`moderate-tag${tags[tag].tagged ? " active" : ""}`}
                      title="Optional label: this scene is deliberately funny (ticket #1502)"
                    >
                      <input
                        type="checkbox"
                        checked={tags[tag].tagged}
                        disabled={tags[tag].busy || tags[tag].tagged}
                        onChange={() => void postTagAction?.(tag)}
                        data-testid={`moderate-${tag}`}
                      />
                      {label}
                    </label>
                  ))}
                </div>
                <RatingControl
                  value={rating.value}
                  busy={rating.busy}
                  onSelect={(v) => void postRatingAction?.(v)}
                  testIdPrefix="moderate-rating"
                />
              </div>
              <div className="moderate-reasons-group">
                <span className="moderate-reasons-label">
                  Reasons (tick any)
                </span>
                <div className="moderate-reasons">
                  {REJECT_REASONS.map((reason) => (
                    <label
                      key={reason}
                      className={`moderate-reason${modReasons.includes(reason) ? " active" : ""}`}
                      title={reason}
                    >
                      <input
                        type="checkbox"
                        checked={modReasons.includes(reason)}
                        disabled={mod.busy || mod.done}
                        onChange={() => toggleModReason(reason)}
                      />
                      {reason}
                    </label>
                  ))}
                </div>
              </div>
              <input
                id="moderate-feedback"
                className="moderate-feedback"
                type="text"
                aria-label="Feedback for the pipeline (optional)"
                placeholder="Feedback for the pipeline (optional)…"
                maxLength={2000}
                value={modFeedback}
                onChange={(e) => setModFeedback(e.target.value)}
              />
              <p
                id="moderate-status"
                className="moderate-status"
                aria-live="polite"
              >
                {mod.status}
              </p>
              {TAGS.map(({ tag, label }) =>
                tags[tag].busy || tags[tag].tagged || tags[tag].error ? (
                  <p
                    key={tag}
                    id={`${tag}-status`}
                    className="moderate-status"
                    aria-live="polite"
                  >
                    {tags[tag].error
                      ? `${label} failed: ${tags[tag].error}`
                      : tags[tag].busy
                        ? `${label} saving…`
                        : `${label} marked.`}
                  </p>
                ) : null,
              )}
              {rating.busy || rating.value !== null || rating.error ? (
                <p
                  id="rating-status"
                  className="moderate-status"
                  aria-live="polite"
                >
                  {rating.error
                    ? `Rating failed: ${rating.error}`
                    : rating.busy
                      ? "Rating saving…"
                      : `Rating ${rating.value}/10.`}
                </p>
              ) : null}
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
 * /anomalyguessr/ mount. Dev-only (ticket #1374): the production Pages bundle
 * compiles every reference to it away, so the public site has neither the page
 * nor a link to it.
 */
const GALLERY_URL = "gallery/";

/**
 * The frontpage (ticket #1214): the game modes as big links, shown before
 * any run. Daily is always there; Moderation and Live only on the dev
 * instance, whose queue API flags its manifest with `moderation: true` and
 * answers scope=live. Daily there previews the next set to ship (#1221),
 * Live replays the set prod serves right now (#1237); the moderation count
 * comes straight from the queue manifest (the unmoderated queue Moderation
 * plays), so the frontend never re-derives an order or a set.
 *
 * Since #1228 each mode is an ordinary anchor to its real path (ticket
 * #1223), not a button that pokes the URL in JS: the browser then owns the
 * link behavior, so middle-click / right-click / Ctrl-click open the mode in
 * a new tab, and a plain click is a normal navigation the reload guard
 * (#1225/#1230) recognizes as deliberate. The frontpage itself stays the
 * bare base URL.
 */
function ModeSelect({
  devMode,
  liveAvailable,
  moderationCount,
  firstRef,
}: {
  devMode: boolean;
  liveAvailable: boolean;
  moderationCount: number;
  firstRef: RefObject<HTMLAnchorElement | null>;
}) {
  return (
    <section id="home" className="home">
      <h2 className="home-title">Choose a mode</h2>
      <div className="modes">
        <a
          id="mode-daily"
          ref={firstRef}
          className="mode-btn"
          href={modePath(APP_BASE, "daily")}
          data-testid="mode-daily"
        >
          <span className="mode-name">Daily</span>
          <span className="mode-sub">
            {devMode ? "The next set to ship" : "Today's set"}
          </span>
        </a>
        {liveAvailable && process.env.NODE_ENV !== "production" ? (
          <a
            id="mode-live"
            className="mode-btn"
            href={modePath(APP_BASE, "live")}
            data-testid="mode-live"
          >
            <span className="mode-name">Live</span>
            <span className="mode-sub">The set on prod right now</span>
          </a>
        ) : null}
        {devMode && process.env.NODE_ENV !== "production" ? (
          <a
            id="mode-moderation"
            className="mode-btn"
            href={modePath(APP_BASE, "moderation")}
            data-testid="mode-moderation"
          >
            <span className="mode-name">Moderation</span>
            <span className="mode-sub">
              {moderationCount > 0
                ? `${moderationCount} ${moderationCount === 1 ? "scene" : "scenes"} waiting for a verdict`
                : "Nothing to review right now"}
            </span>
          </a>
        ) : null}
      </div>
    </section>
  );
}

/**
 * The page header. The title links back to the frontpage (ticket #1220), so
 * a player inside a run can switch modes without reaching for the browser
 * controls; the frontpage itself is the mode menu. Both the title and the
 * gallery link (dev only) are ordinary anchors: the reload guard recognizes
 * a click on them as a deliberate leave (ticket #1230), so no handler is
 * needed here.
 *
 * The gallery link and its URL drop out of the production bundle entirely
 * (ticket #1374): `showMenu` is only true where the queue API serves the dev
 * instance, and the `NODE_ENV` comparison lets the Pages build remove the
 * anchor, so the public site has nothing to link to.
 */
function Header({ showMenu }: { showMenu: boolean }) {
  return (
    <header className="top">
      <div className="top-row">
        <h1>
          <a
            className="home-link"
            href={modePath(APP_BASE, null)}
            title="Back to the mode menu"
            data-testid="home-menu"
          >
            🦊 AnomalyGuessr
          </a>
        </h1>
        {showMenu && process.env.NODE_ENV !== "production" ? (
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
