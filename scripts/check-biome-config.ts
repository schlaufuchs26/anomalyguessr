#!/usr/bin/env bun
// Fail before `biome format --write` can do damage: Biome parses biome.json
// as strict JSON, and a `//` comment (or a trailing comma) makes it discard
// the whole file and fall back to its built-in defaults (tabs, no custom
// rules) without printing an error. A format run then rewrites the project
// with the wrong style and the lint rules silently stop firing (ticket
// #1218). biome.jsonc is the file that may carry comments; use it whenever
// the config needs a comment.
//
// The check is a real JSON.parse, not a grep for `//`: every valid config
// contains a `$schema` URL with `//` in it.
//
// Usage: bun scripts/check-biome-config.ts [root]
//   Scans <root> (default: this repo) for biome.json files and exits 1 when
//   one does not parse. Biome picks up a config from the working directory
//   or above it, so nested configs (api/biome.json in AnomalyGuessr) count
//   too.

import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";

const root = process.argv[2] ?? join(import.meta.dir, "..");

// Dependencies and build output are not configs anyone maintains here.
const SKIP_DIRS = new Set([
  ".git",
  "node_modules",
  "dist",
  "coverage",
  "test-results",
  "playwright-report",
]);

/** All biome.json files under `dir`, as paths relative to `root`. */
function configFiles(dir: string = root, found: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name)) {
        configFiles(join(dir, entry.name), found);
      }
    } else if (entry.name === "biome.json") {
      found.push(relative(root, join(dir, entry.name)));
    }
  }
  return found;
}

/** Line number of the first comment in `text`, or 0 when there is none. */
function commentLine(text: string): number {
  const index = text
    .split("\n")
    .findIndex((line) => /^\s*(\/\/|\/\*)/.test(line));
  return index < 0 ? 0 : index + 1;
}

const broken: string[] = [];
for (const file of configFiles()) {
  const text = readFileSync(join(root, file), "utf8");
  try {
    JSON.parse(text);
  } catch (err) {
    const line = commentLine(text);
    const where = line > 0 ? `, comment on line ${line}` : "";
    broken.push(`${file}${where}: ${(err as Error).message}`);
  }
}

if (broken.length > 0) {
  console.error(
    "✖ biome.json is not strict JSON, so Biome silently ignores it and falls back to its built-in defaults.",
  );
  for (const problem of broken) {
    console.error(`  ${problem}`);
  }
  console.error(
    "Fix: rename the file to biome.jsonc (comments are allowed there) or drop the comment/trailing comma.",
  );
  process.exit(1);
}
