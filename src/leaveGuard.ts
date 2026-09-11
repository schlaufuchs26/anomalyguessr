/**
 * Deliberate-leave detection for the reload guard (tickets #1230, #1225).
 *
 * The guard arms a `beforeunload` handler once a run has a score to lose, and
 * a browser prompt is only wanted for unloads the player did not ask for
 * (reload, close, a Back that leaves the app). Clicking one of the app's own
 * links is such an asked-for move: the modes have real paths since #1223, so
 * following the gallery, a mode link (#1228) or the way back to the frontpage
 * (#1220) replaces the document.
 *
 * The guard watches every click in the document instead of each link carrying
 * its own marker, so a link added later is covered the day it ships. A click
 * only counts when it can actually replace this document: primary button, no
 * modifier (a modified click opens a tab and leaves the page running), an
 * anchor with an href, no `download`, and a target that is this browsing
 * context. Marking one of those exclusions would silently disarm the guard
 * for the rest of the run, which is why they matter.
 */
export function isDeliberateNavigation(event: MouseEvent): boolean {
  if (event.button !== 0) return false;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
    return false;
  }
  const target = event.target;
  if (!(target instanceof Element)) return false;
  const anchor = target.closest("a[href]");
  if (!anchor) return false;
  if (anchor.hasAttribute("download")) return false;
  const linkTarget = anchor.getAttribute("target");
  return !linkTarget || linkTarget === "_self";
}
