import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { agUrl } from "../src/gallery/api";
import { FIXTURE, listBody, makeScene, renderPage } from "./galleryFixtures";

// Mutation flows (reject/restore/comment) live in their own file so both
// gallery test files stay under the 500-line gate. They mock the state
// machine: the first GET returns the fixture, the refetch after a mutation
// returns the changed scene.

describe("AnomalyGuessrGalleryPage mutations", () => {
  let origFetch: typeof global.fetch;

  beforeEach(() => {
    origFetch = global.fetch;
  });

  afterEach(() => {
    global.fetch = origFetch;
  });

  test("reject posts, drops the scene from the daily view and shows it under Rejected", async () => {
    let listCalls = 0;
    global.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === agUrl("scenes") && !init?.method) {
        listCalls += 1;
        if (listCalls === 1) {
          return new Response(JSON.stringify(listBody(FIXTURE)), {
            status: 200,
          });
        }
        const scenes = FIXTURE.map((s) =>
          s.id === "f1"
            ? {
                ...s,
                moderation: "rejected" as const,
                rejected: true,
                state: "rejected" as const,
              }
            : s,
        );
        return new Response(JSON.stringify(listBody(scenes, ["f2", "s1"])), {
          status: 200,
        });
      }
      if (url.endsWith("/f1/reject")) {
        return new Response(JSON.stringify({ id: "f1", rejected: true }), {
          status: 200,
        });
      }
      return new Response("not found", { status: 404 });
    }) as unknown as typeof fetch;

    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("td-reject-f1")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("td-reject-f1"));
    await waitFor(() => {
      expect(screen.queryByText("Fresh Market")).not.toBeInTheDocument();
    });
    expect(listCalls).toBeGreaterThanOrEqual(2);

    // reachable through the Rejected chip
    fireEvent.click(screen.getByTestId("td-filter-rejected"));
    expect(screen.getByText("Fresh Market")).toBeInTheDocument();
    expect(screen.getByTestId("td-restore-f1")).toBeInTheDocument();
    expect(screen.getByTestId("td-moderation-f1").textContent).toBe("Rejected");
  });

  test("restore posts and moves the scene back to Unmoderated", async () => {
    let listCalls = 0;
    global.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === agUrl("scenes") && !init?.method) {
        listCalls += 1;
        const scenes = FIXTURE.map((s) =>
          listCalls > 1 && s.id === "r1"
            ? {
                ...s,
                moderation: "unmoderated" as const,
                rejected: false,
                state: "unshown" as const,
              }
            : s,
        );
        return new Response(JSON.stringify(listBody(scenes)), { status: 200 });
      }
      if (url.endsWith("/r1/restore")) {
        return new Response(JSON.stringify({ id: "r1", rejected: false }), {
          status: 200,
        });
      }
      return new Response("not found", { status: 404 });
    }) as unknown as typeof fetch;

    renderPage();
    await waitFor(() =>
      expect(screen.getByText("Fresh Market")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("td-filter-rejected"));
    fireEvent.click(screen.getByTestId("td-restore-r1"));
    await waitFor(() => {
      expect(screen.queryByText("Rejected Street")).not.toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("td-filter-unmoderated"));
    expect(screen.getByText("Rejected Street")).toBeInTheDocument();
    expect(screen.getByTestId("td-moderation-r1").textContent).toBe(
      "Unmoderated",
    );
    expect(screen.getByTestId("td-reject-r1")).toBeInTheDocument();
  });

  test("adding a comment posts it and shows the stored comment", async () => {
    let listCalls = 0;
    let postedBody = "";
    global.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === agUrl("scenes") && !init?.method) {
        listCalls += 1;
        const scenes = FIXTURE.map((s) =>
          listCalls > 1 && s.id === "f1"
            ? {
                ...s,
                comments: [
                  ...s.comments,
                  {
                    text: "can too big",
                    createdAt: "2026-09-08T09:00:00+02:00",
                  },
                ],
              }
            : s,
        );
        return new Response(JSON.stringify(listBody(scenes)), { status: 200 });
      }
      if (url.endsWith("/f1/comments")) {
        postedBody = JSON.parse(String(init?.body)).text as string;
        return new Response(
          JSON.stringify({
            comment: {
              text: "can too big",
              createdAt: "2026-09-08T09:00:00+02:00",
            },
          }),
          { status: 200 },
        );
      }
      return new Response("not found", { status: 404 });
    }) as unknown as typeof fetch;

    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("td-comment-input-f1")).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId("td-comment-input-f1"), {
      target: { value: "  can too big  " },
    });
    fireEvent.click(screen.getByTestId("td-comment-submit-f1"));
    await waitFor(() => {
      expect(screen.getByText("can too big")).toBeInTheDocument();
    });
    expect(postedBody).toBe("can too big");
  });

  test("points counter, defect warning and funny tag render and toggle (#1502)", async () => {
    let postedBody: unknown = null;
    const scene = makeScene({
      id: "p1",
      title: "Points Scene",
      moderation: "accepted",
      checker: {
        score: 7,
        failed: [3, 9],
        reason: "ok",
        points: 4,
        points_total: 5,
      },
      mechanical: { presence: true, tone: false, size: false },
      funny: true,
    });
    global.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === agUrl("scenes") && !init?.method) {
        return new Response(JSON.stringify(listBody([scene], ["p1"])), {
          status: 200,
        });
      }
      if (url.endsWith("/p1/funny")) {
        postedBody = JSON.parse(String(init?.body));
        return new Response(JSON.stringify({ id: "p1", funny: false }), {
          status: 200,
        });
      }
      return new Response("not found", { status: 404 });
    }) as unknown as typeof fetch;

    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("td-points-p1")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("td-points-p1").textContent).toBe("points 4/5");
    expect(screen.getByTestId("td-defects-p1").textContent).toContain(
      "element missing",
    );
    expect(screen.getByTestId("td-funny-p1")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("td-funny-toggle-p1"));
    await waitFor(() => expect(postedBody).toEqual({ tag: false }));
  });
});
