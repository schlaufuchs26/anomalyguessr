/**
 * URL routing for the game modes (ticket #1223, reworked to path routes).
 *
 * The app is served unchanged from two hosts: GitHub Pages (prod, no
 * server-side rewrites) and an nginx alias under `/anomalyguessr/` (dev).
 * Each mode gets a real single-segment path under the app base:
 *
 *   frontpage  `<base>/`                   e.g. `/` or `/anomalyguessr/`
 *   Daily      `<base>/daily`
 *   Moderation `<base>/moderation`
 *   Live       `<base>/live`
 *
 * The app base is the directory the document was loaded from; `<base
 * href="./">` in index.html pins it, so `document.baseURI` (and every
 * relative URL: the stylesheet, the module, `scenes/manifest.json`,
 * `gallery/`) stays correct from a deep-linked path. Paths are
 * single-segment without a trailing slash so the last segment is always the
 * mode and never changes what the base directory is.
 */

/** The game modes the frontpage offers (tickets #1214, #1237). */
export type Mode = "daily" | "moderation" | "live";

/** The app base directory URL (always ends with "/"). */
export function appBase(): URL {
  return new URL(document.baseURI);
}

/** The path a mode lives at, resolved from the app base. */
export function modePath(base: URL, mode: Mode | null): string {
  return new URL(mode === null ? "." : mode, base).pathname;
}

/**
 * The mode a pathname names under this base; null (the frontpage) for the
 * base itself, an unknown path or anything outside the base.
 */
export function modeFromPath(pathname: string, base: URL): Mode | null {
  const basePath = base.pathname;
  if (!pathname.startsWith(basePath)) return null;
  const rest = pathname.slice(basePath.length);
  return rest === "daily" || rest === "moderation" || rest === "live"
    ? rest
    : null;
}

/**
 * The mode to actually start on this host. Moderation and Live exist only
 * where the queue API serves them: a `/moderation` deep link on prod (no dev
 * flag) and a `/live` load whose scope=live fetch failed degrade to the
 * frontpage instead of starting a run over the wrong set. Daily is always
 * there.
 */
export function effectiveMode(
  requested: Mode | null,
  moderationAvailable: boolean,
  liveAvailable: boolean,
): Mode | null {
  if (requested === "moderation" && !moderationAvailable) return null;
  if (requested === "live" && !liveAvailable) return null;
  return requested;
}
