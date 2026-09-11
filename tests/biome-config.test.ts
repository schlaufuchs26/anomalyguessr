import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import biomeConfig from "../biome.json";

const repoRoot = join(import.meta.dir, "..");

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
  // from the CLI version; the noise buried real output (ticket #1231).
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
