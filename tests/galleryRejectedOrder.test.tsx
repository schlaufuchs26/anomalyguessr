import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { agUrl, fmtDateTime } from "../src/gallery/api";
import {
  listBody,
  makeScene,
  mockList,
  renderPage,
  type Scene,
} from "./galleryFixtures";

// The rejected view orders by moderation time, newest first (ticket #1475).
// The ordering is client-side per chip: the API serves one `added`-sorted
// list to every filter and to the game's own views.

function cardOrder(): string[] {
  return screen.getAllByTestId(/^td-card-/).map((c) => c.dataset.testid ?? "");
}

/** A fetch mock over a mutable scene list: POST reject/restore updates the
 *  list and stamps each rejection later than the previous one, while GET
 *  serves the current state, so the refetch after a mutation sees it. */
function statefulScenes(initial: Scene[]): typeof fetch {
  let scenes = initial;
  let clock = Date.parse("2026-09-13T18:00:00+02:00");
  return (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === agUrl("scenes") && !init?.method) {
      return new Response(JSON.stringify(listBody(scenes)), { status: 200 });
    }
    const m = /\/scenes\/([^/]+)\/(reject|restore)$/.exec(url);
    if (m && init?.method === "POST") {
      const id = m[1];
      const reject = m[2] === "reject";
      clock += 60_000;
      const at = new Date(clock).toISOString();
      scenes = scenes.map((s) =>
        s.id === id
          ? {
              ...s,
              moderation: reject
                ? ("rejected" as const)
                : ("unmoderated" as const),
              rejected: reject,
              state: reject ? ("rejected" as const) : ("unshown" as const),
              rejectedAt: reject ? at : null,
            }
          : s,
      );
      return new Response(JSON.stringify({ id, rejected: reject }), {
        status: 200,
      });
    }
    return new Response("not found", { status: 404 });
  }) as unknown as typeof fetch;
}

describe("rejected view ordering (ticket #1475)", () => {
  let origFetch: typeof global.fetch;

  beforeEach(() => {
    origFetch = global.fetch;
  });

  afterEach(() => {
    global.fetch = origFetch;
  });

  test("orders the rejected view newest moderation first, legacy last", async () => {
    mockList([
      makeScene({
        id: "mid",
        title: "Mid",
        moderation: "rejected",
        rejectedAt: "2026-09-05T10:00:00+02:00",
      }),
      makeScene({
        id: "old",
        title: "Old",
        moderation: "rejected",
        rejectedAt: "2026-09-01T10:00:00+02:00",
      }),
      makeScene({
        id: "new",
        title: "New",
        moderation: "rejected",
        rejectedAt: "2026-09-10T10:00:00+02:00",
      }),
      // a rejection recorded before the timestamp existed (legacy) sorts last
      makeScene({
        id: "legacy",
        title: "Legacy",
        moderation: "rejected",
        rejectedAt: null,
      }),
    ]);
    renderPage();
    fireEvent.click(screen.getByTestId("td-filter-rejected"));
    await waitFor(() => expect(screen.getByText("New")).toBeInTheDocument());
    expect(cardOrder()).toEqual([
      "td-card-new",
      "td-card-mid",
      "td-card-old",
      "td-card-legacy",
    ]);
  });

  test("the card shows the rejection time, in the modal's format", async () => {
    const at = "2026-09-13T19:46:00+02:00";
    mockList([
      makeScene({
        id: "rn",
        title: "Newly rejected",
        moderation: "rejected",
        rejectedAt: at,
      }),
      makeScene({
        id: "legacy",
        title: "Legacy rejected",
        moderation: "rejected",
        rejectedAt: null,
      }),
    ]);
    renderPage();
    fireEvent.click(screen.getByTestId("td-filter-rejected"));
    await waitFor(() =>
      expect(screen.getByText("Newly rejected")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("td-rejected-at-rn").textContent).toBe(
      ` · rejected ${fmtDateTime(at)}`,
    );
    // no timestamp on record: no rejection time on the card
    expect(
      screen.queryByTestId("td-rejected-at-legacy"),
    ).not.toBeInTheDocument();
  });

  test("rejecting a scene puts it first, a second rejection above it", async () => {
    global.fetch = statefulScenes([
      makeScene({
        id: "r0",
        title: "Rejected first",
        moderation: "rejected",
        rejectedAt: "2026-09-01T10:00:00+02:00",
      }),
      makeScene({
        id: "u1",
        title: "Unmoderated one",
        moderation: "unmoderated",
      }),
      makeScene({
        id: "u2",
        title: "Unmoderated two",
        moderation: "unmoderated",
      }),
    ]);
    renderPage();
    fireEvent.click(screen.getByTestId("td-filter-unmoderated"));
    await waitFor(() =>
      expect(screen.getByTestId("td-reject-u1")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByTestId("td-reject-u1"));
    await waitFor(() =>
      expect(screen.queryByText("Unmoderated one")).not.toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("td-filter-rejected"));
    expect(cardOrder()).toEqual(["td-card-u1", "td-card-r0"]);

    fireEvent.click(screen.getByTestId("td-filter-unmoderated"));
    await waitFor(() =>
      expect(screen.getByTestId("td-reject-u2")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("td-reject-u2"));
    await waitFor(() =>
      expect(screen.queryByText("Unmoderated two")).not.toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("td-filter-rejected"));
    expect(cardOrder()).toEqual(["td-card-u2", "td-card-u1", "td-card-r0"]);
  });

  test("restoring removes a scene and keeps the rest ordered", async () => {
    global.fetch = statefulScenes([
      makeScene({
        id: "rn",
        title: "Rejected newer",
        moderation: "rejected",
        rejectedAt: "2026-09-10T10:00:00+02:00",
      }),
      makeScene({
        id: "ro",
        title: "Rejected older",
        moderation: "rejected",
        rejectedAt: "2026-09-01T10:00:00+02:00",
      }),
    ]);
    renderPage();
    fireEvent.click(screen.getByTestId("td-filter-rejected"));
    await waitFor(() =>
      expect(screen.getByText("Rejected newer")).toBeInTheDocument(),
    );
    expect(cardOrder()).toEqual(["td-card-rn", "td-card-ro"]);

    fireEvent.click(screen.getByTestId("td-restore-rn"));
    await waitFor(() =>
      expect(screen.queryByText("Rejected newer")).not.toBeInTheDocument(),
    );
    expect(cardOrder()).toEqual(["td-card-ro"]);
  });
});
