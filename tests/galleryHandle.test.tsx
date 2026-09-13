import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SceneHandle } from "../src/gallery/SceneHandle";
import { SceneModal } from "../src/gallery/SceneModal";
import { makeScene } from "./galleryFixtures";

// The chip is click-to-copy; happy-dom has no clipboard, so each test stubs
// one and the original is restored afterwards.
let originalClipboard: Clipboard | undefined;

beforeEach(() => {
  originalClipboard = navigator.clipboard;
});

afterEach(() => {
  Object.defineProperty(navigator, "clipboard", {
    value: originalClipboard,
    configurable: true,
  });
});

function stubClipboard(): string[] {
  const written: string[] = [];
  Object.defineProperty(navigator, "clipboard", {
    value: {
      writeText: async (text: string) => {
        written.push(text);
      },
    },
    configurable: true,
  });
  return written;
}

describe("SceneHandle", () => {
  test("shows the short id and copies it on click", async () => {
    const written = stubClipboard();
    render(
      <SceneHandle shortId="AG-137" id="commons-long-id" placement="card" />,
    );
    const chip = screen.getByTestId("td-handle-card-commons-long-id");
    expect(chip.textContent).toBe("AG-137");
    // the long id stays reachable as the tooltip
    expect(chip.getAttribute("title")).toContain("commons-long-id");

    fireEvent.click(chip);
    await waitFor(() => expect(written).toEqual(["AG-137"]));
    // the confirmation is transient; it must not become a second label
    await waitFor(() => expect(chip.textContent).toBe("AG-137 ✓"));
  });

  test("renders nothing for a scene without a handle", () => {
    render(<SceneHandle id="legacy-scene" placement="card" />);
    expect(screen.queryByTestId("td-handle-card-legacy-scene")).toBeNull();
  });
});

/** The modal queries through react-query (the trace panel), so render inside
 *  a client like the gallery does. */
function renderModal(shortId?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SceneModal
        scene={makeScene({ id: "h1", shortId, title: "Handled Scene" })}
        onClose={() => {}}
      />
    </QueryClientProvider>,
  );
}

describe("SceneHandle in the expanded modal (ticket #1433)", () => {
  test("the handle is visible in the modal header and in the image view", () => {
    renderModal("AG-118");

    // header: next to the title
    expect(screen.getByTestId("td-handle-modal-h1").textContent).toBe("AG-118");
    // image view: in the tab row, on screen while the photo is judged
    const view = screen.getByTestId("td-handle-view-h1");
    expect(view.textContent).toBe("AG-118");
    expect(view.closest(".td-tabs-row")).not.toBeNull();
  });

  test("the view chip copies the handle too", async () => {
    const written = stubClipboard();

    renderModal("AG-118");
    fireEvent.click(screen.getByTestId("td-handle-view-h1"));
    await waitFor(() => expect(written).toEqual(["AG-118"]));
    expect(screen.getByTestId("td-handle-view-h1").textContent).toBe(
      "AG-118 ✓",
    );
  });

  test("a legacy scene without a handle renders no chip in the modal", () => {
    renderModal(undefined);
    expect(screen.queryByTestId("td-handle-modal-h1")).toBeNull();
    expect(screen.queryByTestId("td-handle-view-h1")).toBeNull();
  });
});
