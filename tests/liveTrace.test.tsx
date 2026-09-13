import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ModerationEmpty } from "../src/ModerationEmpty";

/**
 * The live generation trace under the moderation queue's Generate button
 * (ticket #1446): GET /generate now carries the running scene's step
 * metadata, and opening a step fetches its text from /traces/{ref}.
 *
 * The component is rendered on its own with a short poll interval, so the
 * tests drive the poll rather than wait its real two seconds. The fetch stub
 * answers /generate with the mutable `generateStatus` and /traces/* with the
 * mutable `tracePayload`.
 */

/** What the next GET /generate answers with (the poll reads this). */
let generateStatus: unknown;
/** What /traces/{ref} answers with; "404" means the trace is gone. */
let tracePayload: unknown = "404";
/** Trace URLs the component fetched. */
let traceGets: string[] = [];
/** /generate URLs the component fetched (GET and POST). */
let generateGets: string[] = [];

const STEP = (stage: string, extra: Record<string, unknown> = {}) => ({
  stage,
  model: "deepseek/deepseek-v4.1-flash",
  duration_s: 3.2,
  usage: { prompt_tokens: 900, completion_tokens: 120, cost: 0.0003 },
  ...extra,
});

/** The full trace the /traces endpoint answers with (text included). */
const FULL_TRACE = {
  source: "commons-market-abc123",
  calls: [
    {
      ...STEP("proposal", { attempt: 1 }),
      prompt: "Invent ONE anomaly for this photograph.",
      answer: '{"anomaly": "Plastic bottle"}',
    },
  ],
};

function status(over: Record<string, unknown>): unknown {
  return {
    state: "running",
    running: true,
    buffer: 0,
    count: 5,
    planned: 5,
    added: 0,
    failed: 0,
    imageCalls: 0,
    ...over,
  };
}

function renderEmpty() {
  return render(<ModerationEmpty onReload={() => {}} pollMs={20} />);
}

beforeEach(() => {
  tracePayload = "404";
  traceGets = [];
  generateGets = [];
  generateStatus = status({ state: "idle", running: false });
  globalThis.fetch = (async (input: unknown) => {
    const url = String(input);
    if (url.includes("/anomalyguessr/api/generate")) {
      generateGets.push(url);
      return new Response(JSON.stringify(generateStatus), { status: 200 });
    }
    if (url.includes("/anomalyguessr/api/traces/")) {
      traceGets.push(url);
      if (tracePayload === "404") return new Response("nope", { status: 404 });
      return new Response(JSON.stringify(tracePayload), { status: 200 });
    }
    return new Response("{}", { status: 200 });
  }) as unknown as typeof fetch;
});

afterEach(() => {
  // Nothing to restore: beforeEach installs a fresh stub for every test.
});

describe("live pipeline steps in the empty queue (#1446)", () => {
  test("no live view while no run is being watched", async () => {
    renderEmpty();
    await waitFor(() => expect(generateGets.length).toBeGreaterThan(0));
    expect(screen.queryByTestId("live-trace")).toBeNull();
    expect(screen.queryByTestId("live-trace-waiting")).toBeNull();
  });

  test("a running scene's steps render like the trace panel", async () => {
    generateStatus = status({
      liveTrace: {
        ref: "commons-market-abc123",
        steps: [
          STEP("proposal", { attempt: 1 }),
          STEP("check r0", {
            score: 6,
            failed: [3, 7],
            reason: "the bottle is not readable enough",
          }),
        ],
      },
    });
    renderEmpty();

    await screen.findByTestId("live-trace");
    expect(screen.getByTestId("live-trace-state").textContent).toBe("live");
    expect(screen.getByText(/Anomaly proposal/)).toBeInTheDocument();
    expect(screen.getByText(/Quality check \(round 0\)/)).toBeInTheDocument();
    // the checker's score, failed numbers and one-line reason
    expect(
      screen.getByText(/checker 6\/8 · failed 3, 7 · the bottle is not/),
    ).toBeInTheDocument();
    // the step metadata line
    expect(
      screen.getByText(/deepseek\/deepseek-v4\.1-flash · attempt 1 · 3\.2s/),
    ).toBeInTheDocument();
  });

  test("a second batch appends to the rendered steps", async () => {
    generateStatus = status({
      liveTrace: { ref: "ref-a", steps: [STEP("proposal")] },
    });
    renderEmpty();
    const first = await screen.findByTestId("live-step-0");

    // the pipeline flushed its next step; the next poll carries both
    generateStatus = status({
      liveTrace: {
        ref: "ref-a",
        steps: [STEP("proposal"), STEP("edit r0", { draw: 1, seed: 42 })],
      },
    });
    await screen.findByTestId("live-step-1");
    // the first step's node is reused, not replaced
    expect(screen.getByTestId("live-step-0")).toBe(first);
    expect(screen.getByText(/Image edit \(round 0\)/)).toBeInTheDocument();
  });

  test("opening a step loads its text from the trace endpoint", async () => {
    generateStatus = status({
      liveTrace: {
        ref: "commons-market-abc123",
        steps: [STEP("proposal", { attempt: 1 })],
      },
    });
    tracePayload = FULL_TRACE;
    renderEmpty();

    // prompted text stays out of the live payload
    await screen.findByTestId("live-trace");
    expect(screen.queryByText(/Invent ONE anomaly/)).toBeNull();

    fireEvent.click(screen.getByTestId("live-step-toggle-0"));
    expect(
      await screen.findByText("Invent ONE anomaly for this photograph."),
    ).toBeInTheDocument();
    expect(
      traceGets.some((u) => u.endsWith("traces/commons-market-abc123")),
    ).toBe(true);
  });

  test("a failed text fetch surfaces an error, not a blank step", async () => {
    generateStatus = status({
      liveTrace: { ref: "commons-market-abc123", steps: [STEP("proposal")] },
    });
    renderEmpty();
    await screen.findByTestId("live-trace");

    fireEvent.click(screen.getByTestId("live-step-toggle-0"));
    expect(
      await screen.findByTestId("live-step-text-error"),
    ).toBeInTheDocument();
  });

  test("the run ends: the last steps stay and the polling stops", async () => {
    generateStatus = status({
      liveTrace: { ref: "ref-a", steps: [STEP("proposal"), STEP("check r0")] },
    });
    renderEmpty();
    await screen.findByTestId("live-step-1");

    // the run finishes and the status no longer names an active scene
    generateStatus = status({ state: "done", running: false, added: 1 });
    await waitFor(() =>
      expect(screen.getByTestId("live-trace-state").textContent).toBe(
        "finished",
      ),
    );
    expect(screen.getByTestId("live-step-1")).toBeInTheDocument();

    const polls = generateGets.length;
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(generateGets.length).toBe(polls);
  });

  test("a run that dies shows the last step and the error, not a spinner", async () => {
    generateStatus = status({
      scene: "commons-market-abc123",
      liveTrace: { ref: "commons-market-abc123", steps: [STEP("edit r0")] },
    });
    renderEmpty();
    await screen.findByTestId("live-step-0");

    // the status file still says running, but the run lock is gone: the API
    // reports an error and its steps stay visible
    generateStatus = status({
      state: "error",
      running: false,
      error: "generation run ended unexpectedly",
      liveTrace: { ref: "commons-market-abc123", steps: [STEP("edit r0")] },
    });
    await waitFor(() =>
      expect(screen.getByTestId("live-trace-state").textContent).toBe("failed"),
    );
    expect(
      screen.getByText("generation run ended unexpectedly"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("live-step-0")).toBeInTheDocument();
    expect(screen.queryByTestId("live-trace-waiting")).toBeNull();

    const polls = generateGets.length;
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(generateGets.length).toBe(polls);
  });

  test("after the run, a step opens the landed scene's finished trace", async () => {
    generateStatus = status({
      liveTrace: { ref: "commons-market-abc123", steps: [STEP("proposal")] },
    });
    renderEmpty();
    await screen.findByTestId("live-trace");

    // the scene landed: the pending file became the scene's sidecar
    generateStatus = status({
      state: "done",
      running: false,
      sceneId: "ag-42-plastic-bottle",
      added: 1,
    });
    await waitFor(() =>
      expect(screen.getByTestId("live-trace-state").textContent).toBe(
        "finished",
      ),
    );

    tracePayload = {
      calls: [
        {
          ...STEP("proposal"),
          prompt: "Invent ONE anomaly for this photograph.",
        },
      ],
    };
    fireEvent.click(screen.getByTestId("live-step-toggle-0"));
    await screen.findByText("Invent ONE anomaly for this photograph.");
    expect(
      traceGets.some((u) => u.endsWith("traces/ag-42-plastic-bottle")),
    ).toBe(true);
  });

  test("a malformed live trace falls back to the waiting state", async () => {
    generateStatus = status({ liveTrace: { steps: [] } }); // no ref
    renderEmpty();
    await screen.findByTestId("live-trace-waiting");

    generateStatus = status({ liveTrace: "junk" });
    await waitFor(() => expect(screen.queryByTestId("live-trace")).toBeNull());
  });
});
