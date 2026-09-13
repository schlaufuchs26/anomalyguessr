import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { agUrl } from "../src/gallery/api";
import { SceneModal } from "../src/gallery/SceneModal";
import { makeScene, TRACE, TRACE_GUARDED } from "./galleryFixtures";

// The scene lightbox's generation-trace panel (ticket #1373): collapsed by
// default, fetched only on open, and "no trace recorded" for old scenes.

function renderModal(hasTrace: boolean) {
  const scene = { ...makeScene({ id: "tr", title: "Trace Scene" }), hasTrace };
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SceneModal scene={scene} onClose={() => {}} />
    </QueryClientProvider>,
  );
}

describe("SceneModal generation trace (ticket #1373)", () => {
  let origFetch: typeof global.fetch;

  beforeEach(() => {
    origFetch = global.fetch;
  });

  afterEach(() => {
    global.fetch = origFetch;
  });

  test("a scene with a trace fetches it only when the panel is opened", async () => {
    let calls = 0;
    global.fetch = (async (input: RequestInfo | URL) => {
      calls += 1;
      expect(String(input)).toBe(agUrl("traces/tr"));
      return new Response(JSON.stringify(TRACE), { status: 200 });
    }) as unknown as typeof fetch;

    renderModal(true);

    // collapsed: the toggle exists, no trace content and no request yet
    const toggle = screen.getByTestId("td-trace-toggle-tr");
    expect(toggle).toBeInTheDocument();
    expect(screen.queryByText("Anomaly proposal")).not.toBeInTheDocument();
    expect(calls).toBe(0);

    fireEvent.click(toggle);

    await waitFor(() =>
      expect(screen.getByText("Anomaly proposal")).toBeInTheDocument(),
    );
    expect(calls).toBe(1);
    // the step shows its reasoning and token/cost line
    expect(screen.getByText(/predates PET/)).toBeInTheDocument();
    expect(screen.getByText(/1110 tok/)).toBeInTheDocument();
  });

  test("a scene without a trace shows 'no trace recorded' and does not fetch", () => {
    let calls = 0;
    global.fetch = (async () => {
      calls += 1;
      return new Response("not found", { status: 404 });
    }) as unknown as typeof fetch;

    renderModal(false);
    expect(screen.getByTestId("td-trace-empty-tr").textContent).toBe(
      "no trace recorded",
    );
    expect(screen.getByTestId("td-trace-toggle-tr")).toBeDisabled();
    expect(calls).toBe(0);
  });

  test("a failed trace fetch surfaces an error, not a blank panel", async () => {
    global.fetch = (async () =>
      new Response("nope", { status: 500 })) as unknown as typeof fetch;

    renderModal(true);
    fireEvent.click(screen.getByTestId("td-trace-toggle-tr"));

    await waitFor(() =>
      expect(screen.getByTestId("td-trace-loaderror-tr")).toBeInTheDocument(),
    );
  });
});

// The score guard (ticket #1461) ships an earlier round than the newest one;
// the panel marks which render went out (ticket #1465), so moderation does
// not have to read the JSON for it.
describe("SceneModal trace shipped marker (ticket #1465)", () => {
  let origFetch: typeof global.fetch;

  beforeEach(() => {
    origFetch = global.fetch;
  });

  afterEach(() => {
    global.fetch = origFetch;
  });

  async function openTrace(trace: object) {
    global.fetch = (async (input: RequestInfo | URL) => {
      expect(String(input)).toBe(agUrl("traces/tr"));
      return new Response(JSON.stringify(trace), { status: 200 });
    }) as unknown as typeof fetch;
    renderModal(true);
    fireEvent.click(screen.getByTestId("td-trace-toggle-tr"));
    await waitFor(() =>
      expect(screen.getByText("Anomaly proposal")).toBeInTheDocument(),
    );
  }

  test("marks the kept round shipped and the later rounds superseded", async () => {
    await openTrace(TRACE_GUARDED);

    // step 2 is "check r0", the round the guard kept (score 6) over r2 (4)
    expect(screen.getByTestId("td-trace-marker-tr-2").textContent).toBe(
      "shipped",
    );
    // r1 and r2 stayed in the trace but did not ship
    for (const i of [3, 4, 5, 6]) {
      expect(screen.getByTestId(`td-trace-marker-tr-${i}`).textContent).toBe(
        "superseded",
      );
    }
    // steps outside the correction chain (proposal, edit r0, coordinates)
    // carry no marker
    for (const i of [0, 1, 7]) {
      expect(screen.queryByTestId(`td-trace-marker-tr-${i}`)).toBeNull();
    }
  });

  test("the notes carry the guard's one-line summary", async () => {
    await openTrace(TRACE_GUARDED);

    expect(screen.getByTestId("td-trace-notes").textContent).toContain(
      "score guard: shipped round 0 (checker 6/8) over round 2 (checker 4/8)",
    );
  });

  test("a trace without a guard shows no marker", async () => {
    await openTrace(TRACE);

    expect(screen.queryByText("shipped")).toBeNull();
    expect(screen.queryByText("superseded")).toBeNull();
  });
});
