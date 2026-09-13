import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { agUrl } from "../src/gallery/api";
import { SceneModal } from "../src/gallery/SceneModal";
import { makeScene, TRACE } from "./galleryFixtures";

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
