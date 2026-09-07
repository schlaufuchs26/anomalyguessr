export type Difficulty = "dezent" | "klassisch" | "auffaellig";

const DIFFICULTIES: Difficulty[] = ["dezent", "klassisch", "auffaellig"];

export const DIFFICULTY_LABELS: Record<Difficulty, string> = {
  dezent: "Dezent",
  klassisch: "Klassisch",
  auffaellig: "Auffällig",
};

export interface Scene {
  id: string;
  title: string;
  place: string;
  year: string;
  credit: string;
  sourceUrl: string;
  difficulty: Difficulty;
  /** Relative path to the tampered image (one planted anomaly). */
  image: string;
  /** Relative path to the untouched original, shown after the guess. */
  original: string;
  /** Short label of the planted object, e.g. "Plastikflasche". */
  anomaly: string;
  /** Normalized answer position (0..1, top-left origin) and hit radius. */
  answer: { x: number; y: number; r: number };
  /** Progressive hints, coarse to precise. The last one names the object. */
  hints: [string, string, string];
}

export interface Manifest {
  version: number;
  scenes: Scene[];
}

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

function isDifficulty(v: unknown): v is Difficulty {
  return typeof v === "string" && (DIFFICULTIES as string[]).includes(v);
}

function parseScene(raw: unknown, index: number): Scene {
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
  if (!isDifficulty(raw.difficulty))
    fail(where, "difficulty must be dezent|klassisch|auffaellig");
  return {
    id: reqString(raw.id, where, "id"),
    title: reqString(raw.title, where, "title"),
    place: reqString(raw.place, where, "place"),
    year: reqString(raw.year, where, "year"),
    credit: reqString(raw.credit, where, "credit"),
    sourceUrl: reqString(raw.sourceUrl, where, "sourceUrl"),
    difficulty: raw.difficulty,
    image: reqString(raw.image, where, "image"),
    original: reqString(raw.original, where, "original"),
    anomaly: reqString(raw.anomaly, where, "anomaly"),
    answer: { x: nx, y: ny, r: nr },
    hints: [hints[0] as string, hints[1] as string, hints[2] as string],
  };
}

/** Validates a parsed JSON manifest; throws on malformed entries. */
export function parseManifest(raw: unknown): Manifest {
  if (!isRecord(raw)) fail("root", "not an object");
  if (raw.version !== 1)
    fail("root", `unsupported version ${String(raw.version)}`);
  if (!Array.isArray(raw.scenes) || raw.scenes.length === 0)
    fail("root", "scenes must be a non-empty array");
  return { version: 1, scenes: raw.scenes.map(parseScene) };
}
