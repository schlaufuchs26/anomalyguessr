import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { agUrl } from "../src/gallery/api";
import { FIXTURE, listBody, mockList, renderPage } from "./galleryFixtures";

describe("AnomalyGuessrGalleryPage", () => {
  let origFetch: typeof global.fetch;

  beforeEach(() => {
    origFetch = global.fetch;
  });

  afterEach(() => {
    global.fetch = origFetch;
  });

  test("shows loading state initially", () => {
    global.fetch = (() => new Promise(() => {})) as unknown as typeof fetch;
    renderPage();
    expect(screen.getByText("Loading...")).toBeInTheDocument();
  });

  test("lands on the daily order with the upcoming set marked (ticket #1208)", async () => {
    mockList();
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Fresh Market")).toBeInTheDocument();
    });

    // card order follows the API's dailyOrder
    const cards = screen
      .getAllByTestId(/^td-card-/)
      .map((c) => c.dataset.testid);
    expect(cards).toEqual(["td-card-f1", "td-card-f2", "td-card-s1"]);

    // the leading cards are marked as the upcoming daily set
    expect(screen.getByText("Upcoming daily")).toBeInTheDocument();
    expect(screen.getByTestId("td-daily-f1").textContent).toBe("Daily 1");
    expect(screen.getByTestId("td-daily-f2").textContent).toBe("Daily 2");
    expect(screen.getByTestId("td-daily-s1").textContent).toBe("Daily 3");

    // rejected/unmoderated scenes are not in the default view
    expect(screen.queryByText("Rejected Street")).not.toBeInTheDocument();
    expect(screen.queryByText("Unmoderated Lane")).not.toBeInTheDocument();

    // one vocabulary: moderation badges only; the legacy "Excluded" wording
    // must not reappear (#1205, data-layer rename #1207)
    expect(screen.getByTestId("td-moderation-f1").textContent).toBe("Accepted");
    expect(screen.getByTestId("td-moderation-s1").textContent).toBe("Accepted");
    expect(screen.queryByText("Excluded")).not.toBeInTheDocument();
    expect(screen.queryByText("Unshown")).not.toBeInTheDocument();
    expect(screen.queryByText("Shown")).not.toBeInTheDocument();

    // filter bar: daily is the landing chip; no All/Accepted duplicates
    expect(screen.queryByTestId("td-filter-all")).not.toBeInTheDocument();
    expect(screen.queryByTestId("td-filter-accepted")).not.toBeInTheDocument();
    expect(screen.getByTestId("td-filter-daily").textContent).toBe("Daily(3)");
    expect(screen.getByTestId("td-filter-rejected").textContent).toBe(
      "Rejected(1)",
    );
    expect(screen.getByTestId("td-filter-unmoderated").textContent).toBe(
      "Unmoderated(1)",
    );

    // anomaly chip + existing comment thread
    expect(screen.getByText("Digital watch")).toBeInTheDocument();
    expect(screen.getByText("well blended")).toBeInTheDocument();
    // a non-rejected scene offers Reject
    expect(screen.getByTestId("td-reject-f1").textContent).toBe("✕ Reject");
  });

  test("filter chips narrow the grid to rejected/unmoderated", async () => {
    mockList();
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("Fresh Market")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByTestId("td-filter-rejected"));
    expect(screen.getByText("Rejected Street")).toBeInTheDocument();
    expect(screen.queryByText("Fresh Market")).not.toBeInTheDocument();
    expect(screen.getByTestId("td-restore-r1")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("td-filter-unmoderated"));
    expect(screen.getByText("Unmoderated Lane")).toBeInTheDocument();
    expect(screen.queryByText("Rejected Street")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("td-filter-daily"));
    expect(screen.getByText("Fresh Market")).toBeInTheDocument();
    expect(screen.queryByText("Unmoderated Lane")).not.toBeInTheDocument();
  });

  test("lightbox modal shows edited/original/audit views and closes", async () => {
    mockList();
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("Fresh Market")).toBeInTheDocument(),
    );

    // open f1 (no audit) -> no audit tab; switch to original
    fireEvent.click(
      screen.getAllByRole("button", { name: /fresh market/i })[0],
    );
    await waitFor(() =>
      expect(screen.getByTestId("td-modal-close-f1")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("td-imgtab-original-f1")).toBeInTheDocument();
    expect(screen.queryByTestId("td-imgtab-audit-f1")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("td-imgtab-original-f1"));
    const img = screen.getByTestId("td-img-f1") as HTMLImageElement;
    expect(img.src).toContain("/f1/original");

    // close
    fireEvent.click(screen.getByTestId("td-modal-close-f1"));
    await waitFor(() => {
      expect(screen.queryByTestId("td-modal-close-f1")).not.toBeInTheDocument();
    });

    // open s1 (has audit) -> audit tab appears and switches to the crop
    fireEvent.click(screen.getAllByRole("button", { name: /old market/i })[0]);
    await waitFor(() =>
      expect(screen.getByTestId("td-modal-close-s1")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("td-imgtab-audit-s1"));
    const img2 = screen.getByTestId("td-img-s1") as HTMLImageElement;
    expect(img2.src).toContain("/s1/audit");
  });

  test("hamburger opens an all-photos overview of every scene (ticket #1162)", async () => {
    mockList();
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("Fresh Market")).toBeInTheDocument(),
    );

    // no overview before opening
    expect(screen.queryByTestId("td-overview-overlay")).not.toBeInTheDocument();

    // the overview lists all five scenes incl. the rejected/unmoderated ones
    fireEvent.click(screen.getByTestId("td-menu-btn"));
    await waitFor(() =>
      expect(screen.getByTestId("td-overview-overlay")).toBeInTheDocument(),
    );
    for (const id of ["f1", "f2", "s1", "r1", "u1"]) {
      expect(screen.getByTestId(`td-overview-item-${id}`)).toBeInTheDocument();
    }
    expect(screen.getByText("All photos · 5")).toBeInTheDocument();
    expect(screen.getByTestId("td-overview-mod-r1").textContent).toBe(
      "Rejected",
    );

    // close via the ✕ button
    fireEvent.click(screen.getByTestId("td-overview-close"));
    await waitFor(() => {
      expect(
        screen.queryByTestId("td-overview-overlay"),
      ).not.toBeInTheDocument();
    });
  });

  test("hamburger overview: clicking a thumbnail opens that scene's lightbox (ticket #1162)", async () => {
    mockList();
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("Fresh Market")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByTestId("td-menu-btn"));
    await waitFor(() =>
      expect(screen.getByTestId("td-overview-overlay")).toBeInTheDocument(),
    );

    // jump to the unmoderated scene from the overview
    fireEvent.click(screen.getByTestId("td-overview-item-u1"));
    await waitFor(() => {
      expect(screen.getByTestId("td-modal-close-u1")).toBeInTheDocument();
    });
    // the overview is gone and the lightbox for the chosen scene is open
    expect(screen.queryByTestId("td-overview-overlay")).not.toBeInTheDocument();
    expect(screen.getByTestId("td-imgtab-original-u1")).toBeInTheDocument();
  });

  test("daily view degrades to the API list order when dailyOrder is absent", async () => {
    // A server predating #1208 (needs /restart) serves no dailyOrder field:
    // the view must still show accepted scenes.
    global.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === agUrl("scenes")) {
        const body = listBody(FIXTURE);
        return new Response(
          JSON.stringify({ summary: body.summary, scenes: body.scenes }),
          { status: 200 },
        );
      }
      return new Response("not found", { status: 404 });
    }) as unknown as typeof fetch;

    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Fresh Market")).toBeInTheDocument();
    });
    expect(screen.getByText("Old Market")).toBeInTheDocument();
    // f1 and f2 are still marked as the upcoming daily set
    expect(screen.getByTestId("td-daily-f1")).toBeInTheDocument();
    expect(screen.getByTestId("td-daily-f2")).toBeInTheDocument();
  });
});

describe("short scene handles (#1413)", () => {
  let origFetch: typeof global.fetch;
  let origClipboard: Clipboard | undefined;

  beforeEach(() => {
    origFetch = global.fetch;
    origClipboard = navigator.clipboard;
  });

  afterEach(() => {
    global.fetch = origFetch;
    Object.defineProperty(navigator, "clipboard", {
      value: origClipboard,
      configurable: true,
    });
  });

  test("a card shows the handle and a click copies it", async () => {
    const written: string[] = [];
    Object.defineProperty(navigator, "clipboard", {
      value: {
        writeText: async (text: string) => {
          written.push(text);
        },
      },
      configurable: true,
    });
    mockList();
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("Fresh Market")).toBeInTheDocument(),
    );
    const chip = screen.getByTestId("td-handle-card-f1");
    expect(chip.textContent).toBe("AG-3");
    fireEvent.click(chip);
    await waitFor(() => expect(written).toEqual(["AG-3"]));
  });
});
