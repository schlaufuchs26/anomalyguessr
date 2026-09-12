/**
 * The public-build guard (ticket #1374): the scanner behind
 * `bun scripts/check-public-build.ts` must flag a bundle that still carries a
 * dev-only surface and pass a clean one. The build itself runs the real check
 * against the Pages dist, so this test only pins the scanner's behavior.
 */

import { afterEach, describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  assertPublicBuild,
  scanPublicBuild,
} from "../scripts/check-public-build";

const dirs: string[] = [];

/** A throwaway dist tree; removed after the test. */
function makeDist(files: Record<string, string>): string {
  const dir = mkdtempSync(join(tmpdir(), "ag-public-build-"));
  dirs.push(dir);
  writeFileSync(join(dir, "index.html"), "<!doctype html>");
  for (const [rel, content] of Object.entries(files)) {
    const full = join(dir, rel);
    mkdirSync(join(full, ".."), { recursive: true });
    writeFileSync(full, content);
  }
  return dir;
}

afterEach(() => {
  for (const dir of dirs.splice(0))
    rmSync(dir, { recursive: true, force: true });
});

describe("public build scanner (#1374)", () => {
  test("a clean bundle passes", () => {
    const dist = makeDist({
      "index-abc.js": "console.log('daily quiz')",
      "index-abc.css": ".game{}",
    });
    expect(scanPublicBuild(dist)).toEqual([]);
    expect(() => assertPublicBuild(dist)).not.toThrow();
  });

  test("a remaining gallery link is reported with its file", () => {
    const dist = makeDist({ "index-abc.js": "const g='gallery/'" });
    const found = scanPublicBuild(dist);
    expect(found).toHaveLength(1);
    expect(found[0]?.file).toBe("index-abc.js");
    expect(found[0]?.marker.marker).toBe("gallery");
    expect(() => assertPublicBuild(dist)).toThrow(/dev-only surfaces/);
  });

  test("the queue API prefix and the dev mode links are flagged", () => {
    const dist = makeDist({
      "index-abc.js":
        "fetch('/anomalyguessr/api/generate'); id='mode-live'; id='mode-moderation'",
    });
    const markers = scanPublicBuild(dist)
      .map((v) => v.marker.marker)
      .sort();
    expect(markers).toEqual([
      "anomalyguessr/api/",
      "mode-live",
      "mode-moderation",
    ]);
  });

  test("scene JSON data is not scanned", () => {
    // Markers live in code files; scene metadata cannot trip the check.
    const dist = makeDist({
      "scenes/manifest.json": '{"title":"Gallery 1900"}',
    });
    expect(scanPublicBuild(dist)).toEqual([]);
  });

  test("a dist/gallery directory fails the build", () => {
    const dist = makeDist({});
    mkdirSync(join(dist, "gallery"));
    writeFileSync(join(dist, "gallery", "index.html"), "<!doctype html>");
    expect(() => assertPublicBuild(dist)).toThrow(/gallery/);
  });

  test("a missing dist fails instead of passing silently", () => {
    expect(() =>
      assertPublicBuild(join(tmpdir(), "ag-does-not-exist")),
    ).toThrow(/no public build/);
  });
});
