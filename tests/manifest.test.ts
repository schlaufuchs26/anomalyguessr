import { describe, expect, test } from "bun:test";
import { type Manifest, parseManifest } from "../manifest";

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
    expect(() => parseManifest({ version: 2, scenes: [] })).toThrow(
      /unsupported version/,
    );
    expect(() => parseManifest({ version: 1, scenes: [] })).toThrow(/scenes/);
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
  });
});
