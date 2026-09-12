import { afterEach, describe, expect, test } from "bun:test";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import biomeConfig from "../biome.json";

const repoRoot = join(import.meta.dir, "..");
const guard = join(repoRoot, "scripts", "check-biome-config.ts");

/** Run the guard on a directory; the guard exits non-zero on a bad config. */
function runGuard(root?: string) {
  return Bun.spawnSync(
    [process.execPath, guard, ...(root === undefined ? [] : [root])],
    { cwd: repoRoot, stdout: "pipe", stderr: "pipe" },
  );
}

/** Create a throwaway directory; removed after each test. */
const tempDirs: string[] = [];
function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), "biome-guard-"));
  tempDirs.push(dir);
  return dir;
}

/** The Biome CLI that `bun run biome` actually executes. */
function installedBiomeVersion(): string {
  const pkg = readFileSync(
    join(repoRoot, "node_modules/@biomejs/biome/package.json"),
    "utf8",
  );
  const { version } = JSON.parse(pkg) as { version: string };
  return version;
}

describe("biome.json", () => {
  // Biome prints an "info" on every check when the $schema version differs
  // from the CLI version; the noise buried real output (ticket #1231, #1233).
  test("$schema matches the installed Biome CLI", () => {
    const match = /\$schema.*schemas\/(\d+\.\d+\.\d+)\/schema\.json/.exec(
      JSON.stringify(biomeConfig),
    );
    expect(match?.[1]).toBe(installedBiomeVersion());
  });

  // `linter.rules.recommended` is deprecated since Biome 2.5 in favour of
  // `linter.rules.preset`.
  test("uses the linter rules preset, not the deprecated recommended flag", () => {
    const rules = biomeConfig.linter.rules as Record<string, unknown>;
    expect(rules.recommended).toBeUndefined();
    expect(rules.preset).toBe("recommended");
  });
});

describe("check-biome-config guard", () => {
  afterEach(() => {
    for (const dir of tempDirs.splice(0)) {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  // Biome parses biome.json as strict JSON; a `//` comment makes it discard
  // the file and fall back to defaults without printing anything (ticket
  // #1218). The guard has to fail before `bun run format` rewrites the
  // project with the wrong style.
  test("flags a biome.json with a comment", () => {
    const dir = tempDir();
    writeFileSync(
      join(dir, "biome.json"),
      '{\n  // comment\n  "formatter": { "enabled": true }\n}\n',
    );
    const res = runGuard(dir);
    expect(res.exitCode).toBe(1);
    expect(res.stderr.toString()).toContain("biome.jsonc");
  });

  // A grep for `//` would flag every valid config: `$schema` is an https URL.
  test("accepts a biome.json that is strict JSON", () => {
    const dir = tempDir();
    writeFileSync(
      join(dir, "biome.json"),
      '{\n  "$schema": "https://biomejs.dev/schemas/2.5.13/schema.json"\n}\n',
    );
    const res = runGuard(dir);
    expect(res.exitCode, res.stderr.toString()).toBe(0);
  });

  test("accepts comments in biome.jsonc", () => {
    const dir = tempDir();
    writeFileSync(
      join(dir, "biome.jsonc"),
      '{\n  // comments are fine here\n  "formatter": { "enabled": true }\n}\n',
    );
    const res = runGuard(dir);
    expect(res.exitCode, res.stderr.toString()).toBe(0);
  });

  // A nested config (AnomalyGuessr keeps one under api/) is ignored the same
  // way when Biome runs in that subdirectory.
  test("flags a nested biome.json with a comment", () => {
    const dir = tempDir();
    mkdirSync(join(dir, "api"));
    writeFileSync(join(dir, "api", "biome.json"), "{ // nope\n}\n");
    const res = runGuard(dir);
    expect(res.exitCode).toBe(1);
    expect(res.stderr.toString()).toContain("api/biome.json");
  });

  test("passes on this repo's own config", () => {
    const res = runGuard();
    expect(res.exitCode, res.stderr.toString()).toBe(0);
  });
});
