#!/usr/bin/env bun
// Guard the public/dev build split (ticket #1374).
//
// The same frontend source serves two hosts: GitHub Pages (the public game)
// and the dev instance at fuchs.science/anomalyguessr/. The dev-only surfaces
// (gallery link, Live and Moderation modes, the queue API calls) are gated on
// `process.env.NODE_ENV !== "production"` in the source; the Pages build
// (`bun run build`) defines NODE_ENV=production, so Bun's minifier drops them
// from the bundle and the dev build (`bun run build:dev`) keeps them.
//
// Relying on a minifier to keep a promise is fragile, so this check reads the
// built dist and fails when a marker only dev code emits survives. `build`
// runs it right after the Pages build, so the deploy workflow refuses to
// publish such a bundle; `tests/publicBuild.test.ts` covers the scanner.
//
// Usage: bun scripts/check-public-build.ts [distDir]

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";

/** One dev-only surface and the string its code leaves in a bundle. */
export interface ForbiddenMarker {
  /** Substring that only dev-only code emits. */
  marker: string;
  /** The surface it guards, for the failure message. */
  surface: string;
}

/**
 * Markers that must not survive in the public build. Each is tied to a
 * dev-only surface, not to prose: scene titles or metadata never contain
 * them, so a hit is a real leak.
 */
export const FORBIDDEN_MARKERS: ForbiddenMarker[] = [
  { marker: "gallery", surface: "the gallery link/route" },
  { marker: "anomalyguessr/api/", surface: "the internal queue API prefix" },
  { marker: "mode-live", surface: "the frontpage Live link" },
  { marker: "mode-moderation", surface: "the frontpage Moderation link" },
  {
    marker: "moderation-feedback",
    surface: "the moderation verdict box",
  },
  { marker: "Moderation verdict", surface: "the moderation verdict label" },
  { marker: "scenes/daily.json", surface: "the dev Daily manifest fetch" },
  { marker: "scenes/live.json", surface: "the dev Live manifest fetch" },
];

/** App code the bundler emits. `scenes/*.json` is scene data, not code. */
const CODE_EXTENSIONS = [".js", ".mjs", ".css", ".html"];

/** A forbidden marker found in a built file. */
export interface Violation {
  /** Path of the offending file, relative to the dist root. */
  file: string;
  marker: ForbiddenMarker;
}

/**
 * Every forbidden marker found in the code files under `distDir`, recursively.
 * Empty means the build is clean. Throws when `distDir` does not exist.
 */
export function scanPublicBuild(distDir: string): Violation[] {
  const violations: Violation[] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full);
        continue;
      }
      if (!CODE_EXTENSIONS.some((ext) => entry.name.endsWith(ext))) continue;
      const text = readFileSync(full, "utf8");
      for (const marker of FORBIDDEN_MARKERS) {
        if (text.includes(marker.marker)) {
          violations.push({ file: relative(distDir, full), marker });
        }
      }
    }
  };
  walk(distDir);
  return violations;
}

/** Fail with a readable report when `distDir` is not a clean public build. */
export function assertPublicBuild(distDir: string): void {
  if (!existsSync(join(distDir, "index.html"))) {
    throw new Error(`no public build at ${distDir}: index.html is missing`);
  }
  // The gallery is a page of the dev dashboard, never a file of this build;
  // a dist/gallery directory would mean the split dropped it into the app.
  if (existsSync(join(distDir, "gallery"))) {
    throw new Error(
      `${distDir}/gallery exists: the gallery must not ship in the public build`,
    );
  }
  const violations = scanPublicBuild(distDir);
  if (violations.length > 0) {
    const lines = violations.map(
      (v) => `  ${v.file}: "${v.marker.marker}" (${v.marker.surface})`,
    );
    throw new Error(
      `the public build carries dev-only surfaces:\n${lines.join("\n")}`,
    );
  }
  console.log(
    `public build check: ${distDir} carries no dev-only surface (${FORBIDDEN_MARKERS.length} markers checked)`,
  );
}

if (import.meta.main) {
  const distDir = process.argv[2] ?? join(import.meta.dir, "..", "dist");
  try {
    assertPublicBuild(distDir);
  } catch (err) {
    console.error(`✖ ${(err as Error).message}`);
    process.exit(1);
  }
}
