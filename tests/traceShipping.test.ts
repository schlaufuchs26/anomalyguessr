import { describe, expect, test } from "bun:test";
import {
  scoreGuardNote,
  shippedMarker,
  stepRound,
} from "../src/gallery/TraceSteps";

// The score guard (ticket #1461) ships the best-scoring render of the
// correction chain. These are the pure helpers behind the trace panel's
// shipped/superseded markers (ticket #1465).

const GUARD = { shipped: 0, score: 6, rejected: { round: 2, score: 4 } };
const step = (stage: string) => ({ stage });

describe("stepRound (ticket #1465)", () => {
  test("reads the round off the #1436 stage names", () => {
    expect(stepRound(step("edit r0"))).toBe(0);
    expect(stepRound(step("fix-edit r1"))).toBe(1);
    expect(stepRound(step("check r2"))).toBe(2);
  });

  test("maps the legacy pre-#1436 stages", () => {
    expect(stepRound(step("edit"))).toBe(0);
    expect(stepRound(step("check"))).toBe(0);
    expect(stepRound(step("recheck"))).toBe(1);
  });

  test("null for the steps outside the chain", () => {
    expect(stepRound(step("proposal"))).toBeNull();
    expect(stepRound(step("coordinates"))).toBeNull();
    expect(stepRound(step("click-target 1"))).toBeNull();
    expect(stepRound(step("reconcile-text"))).toBeNull();
  });
});

describe("shippedMarker (ticket #1465)", () => {
  test("only the kept round's check row is 'shipped'", () => {
    expect(shippedMarker(step("check r0"), GUARD)).toBe("shipped");
    // the round's draw was not the last edit of the chain, so it stays plain
    expect(shippedMarker(step("edit r0"), GUARD)).toBeNull();
  });

  test("every step of a later round is 'superseded'", () => {
    expect(shippedMarker(step("fix-edit r1"), GUARD)).toBe("superseded");
    expect(shippedMarker(step("check r1"), GUARD)).toBe("superseded");
    expect(shippedMarker(step("check r2"), GUARD)).toBe("superseded");
  });

  test("steps outside the chain stay unmarked", () => {
    expect(shippedMarker(step("proposal"), GUARD)).toBeNull();
    expect(shippedMarker(step("coordinates"), GUARD)).toBeNull();
  });

  test("no guard record, no markers", () => {
    expect(shippedMarker(step("check r1"))).toBeNull();
    expect(shippedMarker(step("check r1"), undefined)).toBeNull();
  });
});

describe("scoreGuardNote (ticket #1465)", () => {
  test("names the kept round and the score it beat", () => {
    expect(scoreGuardNote(GUARD)).toBe(
      "score guard: shipped round 0 (checker 6/8) over round 2 (checker 4/8)",
    );
  });

  test("tolerates a missing score and a guard without a rejected round", () => {
    expect(scoreGuardNote({ shipped: 1, score: null })).toBe(
      "score guard: shipped round 1 (checker no score)",
    );
  });

  test("null without a guard record", () => {
    expect(scoreGuardNote(undefined)).toBeNull();
  });
});
