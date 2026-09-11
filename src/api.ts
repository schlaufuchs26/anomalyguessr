import type { Manifest } from "../manifest";

/**
 * Single place where the frontend talks to the AnomalyGuessr backend.
 *
 * Ticket #1171 extracts the backend into an independent TypeScript service
 * that serves `/anomalyguessr/api/...` (manifest, scenes, moderate). That
 * service and its nginx location are not wired live yet, so the URLs below
 * stay on the scheme the dev instance serves TODAY: the manifest comes from
 * `scenes/manifest.json` (nginx proxies that path to the queue API on the dev
 * host, and GitHub Pages serves the static ship set), and moderation posts to
 * the existing fuchs2 route. Switching to #1171 is a change in this file
 * only; the game and queue call these functions, never raw URLs.
 */
const MANIFEST_URL = "scenes/manifest.json";

/** Moderation endpoint for the dev workflow (ticket #1163). */
function moderationUrl(sceneId: string): string {
  return `/api/v1/anomalyguessr/scenes/${sceneId}/moderate`;
}

/** Manifest JSON as served: a v1/v2 manifest plus the queue API's flag. */
type ManifestJson = Manifest & { moderation?: boolean };

/** Raw manifest payload: the parsed manifest plus the dev-instance flag. */
export interface LoadedManifest {
  manifest: Manifest;
  /** Queue API marks its payload with `moderation: true` (dev instance). */
  moderation: boolean;
}

/** Load the manifest; throws when the fetch fails. */
export async function loadManifest(
  parse: (raw: unknown) => Manifest,
): Promise<LoadedManifest> {
  const res = await fetch(MANIFEST_URL);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const raw = (await res.json()) as ManifestJson;
  return { manifest: parse(raw), moderation: raw.moderation === true };
}

/** POST a moderation verdict (dev instance only); throws on a non-2xx. */
export async function postModeration(
  sceneId: string,
  action: "accept" | "reject",
  feedback: string,
): Promise<void> {
  const res = await fetch(moderationUrl(sceneId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, feedback }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}
