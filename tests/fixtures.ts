import type { Scene, SceneSource } from "../manifest";

/** Source block required by version-2 manifests (provenance, #1136). */
const SOURCE: SceneSource = {
  repository: "Test repository",
  fileUrl: "https://example.org/file",
  originalTitle: "Test original",
  date: "1900",
  place: "Dresden",
  license: "Public Domain",
  description: "A test scene.",
};

interface SceneOptions {
  id: string;
  title: string;
  x: number;
  y: number;
  r: number;
  explanation?: string;
}

/** A valid version-2 scene with one planted anomaly at (x, y). */
export function makeScene(opts: SceneOptions): Scene {
  return {
    id: opts.id,
    title: opts.title,
    place: "Dresden",
    year: "1900",
    credit: "Public Domain, via Wikimedia Commons",
    sourceUrl: `https://example.org/${opts.id}`,
    source: SOURCE,
    image: `scenes/${opts.id}.jpg`,
    original: `scenes/${opts.id}-original.jpg`,
    anomaly: "Drone",
    description: `Context for ${opts.title}.`,
    explanation: opts.explanation ?? "Drones did not exist in 1900.",
    references: [
      { label: "History of drones", url: "https://example.org/drones" },
    ],
    answer: { x: opts.x, y: opts.y, r: opts.r },
    hints: ["Attached to a person.", "Right half.", "In the sky: a drone."],
  };
}
