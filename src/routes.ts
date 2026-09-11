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
 *
 * The app base is the directory the document was loaded from; `<base
 * href="./">` in index.html pins it, so `document.baseURI` (and every
 * relative URL: the stylesheet, the module, `scenes/manifest.json`,
 * `gallery/`) stays correct from a deep-linked path. Paths are
 * single-segment without a trailing slash so the last segment is always the
 * mode and never changes what the base directory is.
 */

/** The game modes the frontpage offers (ticket #1214). */
export type Mode = "daily" | "moderation";

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
  return rest === "daily" || rest === "moderation" ? rest : null;
}

/**
 * The mode to actually start on this host. Moderation only exists where the
 * manifest carries the dev flag; a `/moderation` deep link on prod degrades
 * to the frontpage instead of an empty game shell.
 */
export function effectiveMode(
  requested: Mode | null,
  moderationAvailable: boolean,
): Mode | null {
  return requested === "moderation" && !moderationAvailable ? null : requested;
}
