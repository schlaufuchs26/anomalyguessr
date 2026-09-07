import { describe, expect, test } from "bun:test";
import { type Manifest, parseManifest, type SceneSource } from "../manifest";

function validScene(): Record<string, unknown> {
  return {
    id: "s1",
    title: "Markt",
    place: "Dresden",
    year: "1900",
    credit: "PD",
    sourceUrl: "https://example.test/file",
    difficulty: "klassisch",
    image: "scenes/s1.jpg",
    original: "scenes/s1-original.jpg",
    anomaly: "Plastikflasche",
    description: "Ein Markt um 1900 mit Ständen und Obst.",
    answer: { x: 0.3, y: 0.6, r: 0.04 },
    hints: [
      "auf einem Wagen",
      "links",
      "zwischen den Kisten: eine Plastikflasche",
    ],
  };
}

function validManifest(sceneOverrides: Record<string, unknown> = {}): unknown {
  return { version: 1, scenes: [{ ...validScene(), ...sceneOverrides }] };
}

describe("parseManifest", () => {
  test("accepts a valid manifest", () => {
    const m: Manifest = parseManifest(validManifest());
    expect(m.version).toBe(1);
    expect(m.scenes).toHaveLength(1);
    expect(m.scenes[0]?.answer.x).toBe(0.3);
    expect(m.scenes[0]?.difficulty).toBe("klassisch");
  });

  test("rejects non-object roots", () => {
    expect(() => parseManifest(null)).toThrow(/not an object/);
    expect(() => parseManifest([])).toThrow(/not an object/);
  });

  test("rejects unsupported versions and empty scene lists", () => {
    expect(() => parseManifest({ version: 3, scenes: [] })).toThrow(
      /unsupported version/,
    );
    expect(() => parseManifest({ version: 1, scenes: [] })).toThrow(/scenes/);
    expect(() => parseManifest({ version: 2, scenes: [] })).toThrow(/scenes/);
  });

  test("rejects answer coordinates outside [0,1]", () => {
    expect(() =>
      parseManifest(validManifest({ answer: { x: 1.2, y: 0.5, r: 0.05 } })),
    ).toThrow(/answer/);
    expect(() =>
      parseManifest(validManifest({ answer: { x: 0.5, y: -0.1, r: 0.05 } })),
    ).toThrow(/answer/);
  });

  test("rejects non-positive or oversized radius", () => {
    expect(() =>
      parseManifest(validManifest({ answer: { x: 0.5, y: 0.5, r: 0 } })),
    ).toThrow(/answer/);
    expect(() =>
      parseManifest(validManifest({ answer: { x: 0.5, y: 0.5, r: 0.9 } })),
    ).toThrow(/answer/);
  });

  test("rejects unknown difficulties", () => {
    expect(() =>
      parseManifest(validManifest({ difficulty: "unsichtbar" })),
    ).toThrow(/difficulty/);
  });

  test("rejects wrong hint counts", () => {
    expect(() => parseManifest(validManifest({ hints: ["a", "b"] }))).toThrow(
      /hints/,
    );
    expect(() =>
      parseManifest(validManifest({ hints: ["a", "b", ""] })),
    ).toThrow(/hints/);
  });

  test("rejects missing required string fields", () => {
    expect(() => parseManifest(validManifest({ image: "" }))).toThrow(/image/);
    expect(() => parseManifest(validManifest({ id: undefined }))).toThrow(/id/);
    expect(() => parseManifest(validManifest({ description: "" }))).toThrow(
      /description/,
    );
  });
});

function validSource(): Record<string, unknown> {
  return {
    repository: "Wikimedia Commons (DPLA)",
    fileUrl: "https://commons.wikimedia.org/wiki/File:Example.jpg",
    originalTitle: "Market vendors, ca. 1907",
    date: "ca. 1907",
    place: "Seattle, USA",
    license: "Public Domain",
    description: "Market vendors gathering on a street.",
  };
}

function v2Manifest(
  sceneOverrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    version: 2,
    date: "2026-09-08",
    scenes: [{ ...validScene(), source: validSource(), ...sceneOverrides }],
  };
}

describe("parseManifest version 2 (pipeline daily manifest)", () => {
  test("accepts a v2 manifest with date + per-scene source", () => {
    const m: Manifest = parseManifest(v2Manifest());
    expect(m.version).toBe(2);
    expect(m.date).toBe("2026-09-08");
    expect(m.scenes).toHaveLength(1);
    expect(m.scenes[0]?.source?.repository).toBe("Wikimedia Commons (DPLA)");
    expect(m.scenes[0]?.source?.place).toBe("Seattle, USA");
  });

  test("rejects a v2 manifest without a date", () => {
    const raw = v2Manifest() as Record<string, unknown>;
    delete raw.date;
    expect(() => parseManifest(raw)).toThrow(/date/);
  });

  test("rejects a v2 manifest with a malformed date", () => {
    expect(() =>
      parseManifest({ ...v2Manifest(), date: "08.09.2026" }),
    ).toThrow(/YYYY-MM-DD/);
  });

  test("rejects v2 scenes without a source block", () => {
    const raw = v2Manifest() as { scenes: Record<string, unknown>[] };
    delete raw.scenes[0]?.source;
    expect(() => parseManifest(raw)).toThrow(/source/);
  });

  test("accepts a v1 manifest with an optional valid date", () => {
    const m = parseManifest({ ...validManifest(), date: "2026-09-07" });
    expect(m.date).toBe("2026-09-07");
  });

  test("rejects a v1 manifest with a malformed date", () => {
    expect(() =>
      parseManifest({ ...validManifest(), date: "07.09.2026" }),
    ).toThrow(/YYYY-MM-DD/);
  });

  test("rejects a malformed source block", () => {
    const raw = v2Manifest({
      source: { repository: "", fileUrl: 42, originalTitle: "t" },
    });
    expect(() => parseManifest(raw)).toThrow(/source\./);
  });

  test("accepts an optional source block on v1 scenes", () => {
    const m = parseManifest(
      validManifest({
        source: { ...validSource(), place: "", description: "" },
      }),
    );
    expect(m.scenes[0]?.source?.place).toBe("");
    expect(m.scenes[0]?.source?.license).toBe("Public Domain");
    // SceneSource is the parsed shape of a valid source record.
    const src: SceneSource = { ...m.scenes[0]?.source } as SceneSource;
    expect(src.originalTitle).toBe("Market vendors, ca. 1907");
  });

  test("rejects a v1 scene with a non-object source", () => {
    expect(() => parseManifest(validManifest({ source: "DPLA" }))).toThrow(
      /source/,
    );
  });
});
