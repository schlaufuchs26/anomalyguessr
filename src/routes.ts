/**
 * URL routing for the game modes (ticket #1223).
 *
 * The app is served unchanged from two static-ish hosts: GitHub Pages (prod,
 * no server-side rewrites) and an nginx alias under `/anomalyguessr/` (dev).
 * A fragment scheme is the only one both survive without server config: the
 * request path stays `/` (prod) or `/anomalyguessr/` (dev), so nothing can
 * 404, the base stays intact for every relative URL (`scenes/manifest.json`,
 * `gallery/`), and `hashchange` gives Back/Forward plus reload persistence
 * for free. Clean path routes (`/daily`) would need a `404.html` redirect on
 * Pages and a `try_files` on nginx; not worth it for two modes.
 */

/** The game modes the frontpage offers (ticket #1214). */
export type Mode = "daily" | "moderation";

/** The fragment for a mode; the frontpage is the empty fragment. */
export function modeHash(mode: Mode | null): string {
  return mode ? `#${mode}` : "";
}

/** The mode a location's fragment names; null (frontpage) for anything else. */
export function modeFromHash(hash: string): Mode | null {
  const value = hash.replace(/^#/, "").toLowerCase();
  return value === "daily" || value === "moderation" ? value : null;
}

/**
 * The mode to actually start on this host. Moderation only exists where the
 * manifest carries the dev flag; a `#moderation` deep link on prod degrades
 * to the frontpage instead of an empty game shell.
 */
export function effectiveMode(
  requested: Mode | null,
  moderationAvailable: boolean,
): Mode | null {
  return requested === "moderation" && !moderationAvailable ? null : requested;
}
