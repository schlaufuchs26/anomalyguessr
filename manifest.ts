/**
 * Provenance of the source photograph, taken from the source's OWN catalog
 * metadata (Wikimedia Commons / DPLA / State Library of Queensland /
 * Fortepan / ... file record), never inferred from the image. Required for
 * pipeline-shipped daily manifests (version 2) so every scene stays
 * historically traceable.
 */
export interface SceneSource {
  /** Human-readable repository name, e.g. "Wikimedia Commons (DPLA)". */
  repository: string;
  /** URL of the catalog record / file page the image came from. */
  fileUrl: string;
  /** The record's own title for the image. */
  originalTitle: string;
  /** The record's own date string (may be a range or "ca."). */
  date: string;
  /** The record's own place string; empty when the record has none. */
  place: string;
  /** License as stated by the record, e.g. "Public Domain". */
  license: string;
  /** The record's own description text; empty when the record has none. */
  description: string;
}

export interface Scene {
  id: string;
  /** Short UI label for the scene (derived from, never contradicting, the source). */
  title: string;
  place: string;
  year: string;
  credit: string;
  sourceUrl: string;
  /** Provenance block; required in version-2 (pipeline) manifests. */
  source?: SceneSource;
  /** Relative path to the tampered image (one planted anomaly). */
  image: string;
  /** Relative path to the untouched original, shown after the guess. */
  original: string;
  /** Short label of the planted anomaly, e.g. "Plastic bottle". */
  anomaly: string;
  /**
   * Why the anomaly could not have been in the original photo.
   * Required for pipeline-shipped daily manifests (version 2); legacy
   * scenes without one stay playable but show no reveal block.
   */
  explanation?: string;
  /**
   * One reference per factual claim in the explanation (Wikipedia or a
   * similar reliable source); rendered as links in the post-guess reveal.
   */
  references?: { label: string; url: string }[];
  /**
   * 1-2 sentence context. Written strictly from the source metadata (and
   * plain visible content): no invented dates, names, events or context.
   */
  description: string;
  /** Normalized answer position (0..1, top-left origin) and hit radius. */
  answer: { x: number; y: number; r: number };
  /** Progressive hints, coarse to precise. The last one names the object. */
  hints: [string, string, string];
}

export interface Manifest {
  /** 1 = legacy pool (frontend date-seeds 5 of N); 2 = pipeline daily set. */
  version: 1 | 2;
  /** Quiz date (YYYY-MM-DD); required for version 2. */
  date?: string;
  scenes: Scene[];
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function fail(where: string, reason: string): never {
  throw new Error(`manifest: ${where}: ${reason}`);
}

function reqString(v: unknown, where: string, key: string): string {
  if (typeof v !== "string" || v.trim() === "")
    fail(where, `${key} must be a non-empty string`);
  return v;
}

function optString(v: unknown, where: string, key: string): string {
  if (v === undefined) return "";
  if (typeof v !== "string") fail(where, `${key} must be a string`);
  return v;
}

function parseSource(raw: unknown, where: string): SceneSource {
  if (!isRecord(raw)) fail(where, "source must be an object");
  return {
    repository: reqString(raw.repository, where, "source.repository"),
    fileUrl: reqString(raw.fileUrl, where, "source.fileUrl"),
    originalTitle: reqString(raw.originalTitle, where, "source.originalTitle"),
    date: reqString(raw.date, where, "source.date"),
    place: optString(raw.place, where, "source.place"),
    license: reqString(raw.license, where, "source.license"),
    description: optString(raw.description, where, "source.description"),
  };
}

function parseScene(
  raw: unknown,
  index: number,
  requireSource: boolean,
): Scene {
  const where = `scenes[${index}]`;
  if (!isRecord(raw)) fail(where, "not an object");
  const answer = raw.answer;
  if (!isRecord(answer)) fail(where, "answer missing");
  const { x, y, r } = answer;
  const num = (v: unknown): number =>
    typeof v === "number" && Number.isFinite(v) ? v : NaN;
  const nx = num(x);
  const ny = num(y);
  const nr = num(r);
  if (nx < 0 || nx > 1 || ny < 0 || ny > 1)
    fail(where, "answer x/y must be in [0,1]");
  if (!(nr > 0 && nr <= 0.5)) fail(where, "answer r must be in (0, 0.5]");
  const hints = raw.hints;
  if (
    !Array.isArray(hints) ||
    hints.length !== 3 ||
    hints.some((h) => typeof h !== "string" || h.trim() === "")
  ) {
    fail(where, "hints must be an array of exactly 3 non-empty strings");
  }
  const explanation = raw.explanation;
  if (
    explanation !== undefined &&
    (typeof explanation !== "string" || explanation.trim() === "")
  )
    fail(where, "explanation must be a non-empty string");
  const references = raw.references;
  if (references !== undefined) {
    if (!Array.isArray(references) || references.length === 0)
      fail(where, "references must be a non-empty array when present");
    for (const r of references) {
      if (!isRecord(r)) fail(where, "each reference must be an object");
      const label = r.label;
      const url = r.url;
      if (typeof label !== "string" || label.trim() === "")
        fail(where, "reference label must be a non-empty string");
      if (typeof url !== "string" || !/^https?:\/\//.test(url))
        fail(where, "reference url must be an http(s) URL");
    }
  }
  const scene: Scene = {
    id: reqString(raw.id, where, "id"),
    title: reqString(raw.title, where, "title"),
    place: reqString(raw.place, where, "place"),
    year: reqString(raw.year, where, "year"),
    credit: reqString(raw.credit, where, "credit"),
    sourceUrl: reqString(raw.sourceUrl, where, "sourceUrl"),
    image: reqString(raw.image, where, "image"),
    original: reqString(raw.original, where, "original"),
    anomaly: reqString(raw.anomaly, where, "anomaly"),
    description: reqString(raw.description, where, "description"),
    answer: { x: nx, y: ny, r: nr },
    hints: [hints[0] as string, hints[1] as string, hints[2] as string],
    ...(explanation !== undefined ? { explanation } : {}),
    ...(references !== undefined
      ? {
          references: (references as { label: string; url: string }[]).map(
            (r) => ({ label: String(r.label), url: String(r.url) }),
          ),
        }
      : {}),
  };
  if (raw.source !== undefined || requireSource) {
    scene.source = parseSource(raw.source, where);
  }
  return scene;
}

/** Validates a parsed JSON manifest; throws on malformed entries. */
export function parseManifest(raw: unknown): Manifest {
  if (!isRecord(raw)) fail("root", "not an object");
  if (raw.version !== 1 && raw.version !== 2)
    fail("root", `unsupported version ${String(raw.version)}`);
  // An empty list is valid: the dev queue serves it when every scene is
  // moderated, and the app renders its empty state for it (ticket #1202).
  if (!Array.isArray(raw.scenes)) fail("root", "scenes must be an array");
  const version = raw.version as 1 | 2;
  const requireSource = version === 2;
  let date: string | undefined;
  if (version === 2) {
    if (raw.date === undefined) fail("root", "date is required for version 2");
    date = reqString(raw.date, "root", "date");
    if (!ISO_DATE.test(date))
      fail("root", `date must be YYYY-MM-DD, got ${date}`);
  } else if (raw.date !== undefined) {
    date = reqString(raw.date, "root", "date");
    if (!ISO_DATE.test(date))
      fail("root", `date must be YYYY-MM-DD, got ${date}`);
  }
  return {
    version,
    ...(date !== undefined ? { date } : {}),
    scenes: raw.scenes.map((s, i) => parseScene(s, i, requireSource)),
  };
}
