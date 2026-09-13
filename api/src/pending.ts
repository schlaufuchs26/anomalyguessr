import path from "node:path";

/**
 * The in-flight generation trace (ticket #1446).
 *
 * pipeline/ag_generate.py flushes the trace of the scene it is working on to
 * data/anomalyguessr/traces/pending/<slug>.json after every step and renames
 * it to the scene id once the scene lands (ag_generate.pending_trace_path,
 * flush_trace/write_trace). The slug rule below mirrors that Python side:
 * lowercase, every character outside [a-z0-9._-] becomes "-", truncated to
 * 120 chars, "source" when nothing is left.
 *
 * The live moderation view names the running scene by this slug, so the slug
 * is a public reference: it is the `ref` path segment of GET /traces/{ref}.
 */

/** Slug of a source id, as the pending trace file is named. */
export function pendingTraceSlug(sourceId: string): string {
  const slug = sourceId
    .toLowerCase()
    .replace(/[^a-z0-9._-]/g, "-")
    .slice(0, 120);
  return slug === "" ? "source" : slug;
}

/**
 * A reference the pending-trace lookup accepts: exactly what the slug rule
 * produces. Anything else (slashes, percent escapes, an uppercase source id
 * that was never a slug) names no pending file.
 */
export const PENDING_REF_RE = /^[a-z0-9._-]{1,120}$/;

/** The pending trace file of `ref` (already a slug). */
export function pendingTracePath(dataDir: string, ref: string): string {
  return path.join(dataDir, "traces", "pending", `${ref}.json`);
}
