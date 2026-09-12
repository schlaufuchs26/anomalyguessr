import { describe, expect, test } from "bun:test";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { handle } from "../src/routes.ts";
import type { Manifest, SceneList } from "../src/scenes.ts";
import type { Moderation } from "../src/types.ts";
import { makeEnv, scene, writeState } from "./helpers.ts";

function req(method: string, url: string, body?: unknown): Request {
  const init: RequestInit = { method };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const full = url.startsWith("http")
    ? url
    : `http://test.local/anomalyguessr/api/${url}`;
  return new Request(full, init);
}

function apiUrl(rest: string, query = ""): string {
  return `http://test.local/anomalyguessr/api/${rest}${query}`;
}

/** Parses a response body with the caller's declared shape. */
async function json<T>(res: Response): Promise<T> {
  return (await res.json()) as T;
}

/** The POST /scenes/{id}/moderate response body. */
interface ModerateResponse {
  id: string;
  moderation: Moderation;
}

describe("manifest", () => {
  test("scope=all: unshown first, then shown, newest added first", async () => {
    const { env } = await makeEnv();
    const res = await handle(
      env,
      "GET",
      "manifest",
      req("GET", apiUrl("manifest")),
    );
    expect(res.status).toBe(200);
    const body = await json<Manifest>(res);
    expect(body.version).toBe(2);
    expect(body.moderation).toBe(true);
    expect(body.scenes.map((s) => s.id)).toEqual([
      "beta-street",
      "alpha-market",
    ]);
    const beta = body.scenes[0];
    expect(beta?.image).toBe("/anomalyguessr/api/scenes/beta-street/image");
    expect(beta?.original).toBe(
      "/anomalyguessr/api/scenes/beta-street/original",
    );
    expect(beta?.source).toBeTruthy();
  });

  test("scopes restrict groups + invalid scope 400", async () => {
    const { env } = await makeEnv();
    const unshown = await json<Manifest>(
      await handle(
        env,
        "GET",
        "manifest",
        req("GET", "manifest?scope=unshown"),
      ),
    );
    expect(unshown.scenes.map((s) => s.id)).toEqual(["beta-street"]);
    const shown = await json<Manifest>(
      await handle(env, "GET", "manifest", req("GET", "manifest?scope=shown")),
    );
    expect(shown.scenes.map((s) => s.id)).toEqual(["alpha-market"]);

    // accept beta -> moves out of unmoderated into accepted
    await handle(
      env,
      "POST",
      "scenes/beta-street/moderate",
      req("POST", "scenes/beta-street/moderate", { action: "accept" }),
    );
    const unmod = await json<Manifest>(
      await handle(
        env,
        "GET",
        "manifest",
        req("GET", "manifest?scope=unmoderated"),
      ),
    );
    expect(unmod.scenes.map((s) => s.id)).toEqual(["alpha-market"]);
    const acc = await json<Manifest>(
      await handle(
        env,
        "GET",
        "manifest",
        req("GET", "manifest?scope=accepted"),
      ),
    );
    expect(acc.scenes.map((s) => s.id)).toEqual(["beta-street"]);

    const bad = await handle(
      env,
      "GET",
      "manifest",
      req("GET", apiUrl("manifest?scope=bogus")),
    );
    expect(bad.status).toBe(400);
  });
});

describe("scenes list", () => {
  test("summaries + order + image URLs + audit", async () => {
    const { env } = await makeEnv();
    const res = await handle(env, "GET", "scenes", req("GET", "scenes"));
    const body = await json<SceneList>(res);
    expect(body.scenes.length).toBe(2);
    expect(body.scenes[0]?.id).toBe("beta-street");
    expect(body.scenes[1]?.id).toBe("alpha-market");
    expect(body.summary).toEqual({
      total: 2,
      unshown: 1,
      shown: 1,
      accepted: 0,
      rejected: 0,
      unmoderated: 2,
      unshownAccepted: 0,
    });
    const alpha = body.scenes.find((s) => s.id === "alpha-market");
    expect(alpha?.state).toBe("shown");
    expect(alpha?.images.edited).toBe(
      "/anomalyguessr/api/scenes/alpha-market/image",
    );
    expect(alpha?.images.audit).toBe(
      "/anomalyguessr/api/scenes/alpha-market/audit",
    );
    const beta = body.scenes.find((s) => s.id === "beta-street");
    expect(beta?.images.audit).toBeUndefined();
    // #1373: the list flags who has a trace sidecar, without inlining it
    expect(alpha?.hasTrace).toBe(true);
    expect(beta?.hasTrace).toBe(false);
  });

  test("summary.unshownAccepted counts only unshown accepted scenes (#1210)", async () => {
    const { env, dir } = await makeEnv();
    await writeState(dir, {
      version: 1,
      last_shipped: "2026-09-07",
      scenes: {
        "alpha-market": scene("alpha-market", "2026-09-01", "2026-09-07"),
        "beta-street": scene("beta-street", "2026-09-08", null),
      },
    });
    // accept the unshown beta (buffer) and the shown alpha (not the buffer)
    for (const id of ["beta-street", "alpha-market"]) {
      await handle(
        env,
        "POST",
        `scenes/${id}/moderate`,
        req("POST", "x", { action: "accept" }),
      );
    }
    const body = await json<SceneList>(
      await handle(env, "GET", "scenes", req("GET", "scenes")),
    );
    expect(body.summary.unshownAccepted).toBe(1);
    expect(body.summary.accepted).toBe(2);
    expect(body.summary.unshown).toBe(1);
  });
});

describe("generation traces", () => {
  test("serves one scene's trace sidecar as JSON", async () => {
    const { env } = await makeEnv();
    const res = await handle(
      env,
      "GET",
      "traces/alpha-market",
      req("GET", apiUrl("traces/alpha-market")),
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toContain("application/json");
    const body = await json<{ calls: { stage: string }[] }>(res);
    expect(body.calls.map((c) => c.stage)).toEqual(["proposal", "edit"]);
  });

  test("404 for a scene without a trace, an unknown id, and path tricks", async () => {
    const { env } = await makeEnv();
    for (const rest of [
      "traces/beta-street", // known scene, no trace recorded
      "traces/ghost-scene",
      "traces/..%2f..%2fstate", // unsafe id
      "traces/Alpha_Market", // fails the slug rule
    ]) {
      const res = await handle(env, "GET", rest, req("GET", apiUrl(rest)));
      expect(res.status).toBe(404);
    }
  });
});

describe("images", () => {
  test("serves edited/original/audit with right content type; 404 for missing/unsafe", async () => {
    const { env } = await makeEnv();
    for (const [kind, want] of [
      ["image", "image/jpeg"],
      ["original", "image/jpeg"],
      ["audit", "image/png"],
    ] as const) {
      const res = await handle(
        env,
        "GET",
        `scenes/alpha-market/${kind}`,
        req("GET", "x"),
      );
      expect(res.status).toBe(200);
      expect(res.headers.get("Content-Type")).toBe(want);
    }
    // beta has no audit -> 404; unknown + unsafe ids -> 404
    expect(
      (await handle(env, "GET", "scenes/beta-street/audit", req("GET", "x")))
        .status,
    ).toBe(404);
    expect(
      (await handle(env, "GET", "scenes/nope/image", req("GET", "x"))).status,
    ).toBe(404);
    expect(
      (await handle(env, "GET", "scenes/../etc/image", req("GET", "x"))).status,
    ).toBe(404);
  });
});

describe("moderation", () => {
  test("accept -> accepted + feedback comment; reject -> rejected; idempotent", async () => {
    const { env } = await makeEnv();
    // unknown -> 404; bad action -> 400
    expect(
      (
        await handle(
          env,
          "POST",
          "scenes/nope/moderate",
          req("POST", "x", { action: "accept" }),
        )
      ).status,
    ).toBe(404);
    expect(
      (
        await handle(
          env,
          "POST",
          "scenes/beta-street/moderate",
          req("POST", "x", { action: "bogus" }),
        )
      ).status,
    ).toBe(400);

    const acc = await json<ModerateResponse>(
      await handle(
        env,
        "POST",
        "scenes/beta-street/moderate",
        req("POST", "x", { action: "accept", feedback: "great blend" }),
      ),
    );
    expect(acc.moderation).toBe("accepted");
    const list = await json<SceneList>(
      await handle(env, "GET", "scenes", req("GET", "scenes")),
    );
    const beta = list.scenes.find((s) => s.id === "beta-street");
    expect(beta?.moderation).toBe("accepted");
    expect(beta?.comments[0]?.text).toBe("great blend");

    const rej = await json<ModerateResponse>(
      await handle(
        env,
        "POST",
        "scenes/beta-street/moderate",
        req("POST", "x", { action: "reject", feedback: "too obvious" }),
      ),
    );
    expect(rej.moderation).toBe("rejected");
    const list2 = await json<SceneList>(
      await handle(env, "GET", "scenes", req("GET", "scenes")),
    );
    const beta2 = list2.scenes.find((s) => s.id === "beta-street");
    expect(beta2?.moderation).toBe("rejected");
    expect(beta2?.rejected).toBe(true);
    expect(beta2?.state).toBe("rejected");
    expect(beta2?.comments.length).toBe(2);
    expect(beta2?.comments[1]?.text).toBe("too obvious");
  });
});

describe("reject/restore + comments", () => {
  test("reject/restore round trip + idempotent + 404 unknown", async () => {
    const { env } = await makeEnv();
    expect(
      (await handle(env, "POST", "scenes/beta-street/reject", req("POST", "x")))
        .status,
    ).toBe(200);
    let list = await json<SceneList>(
      await handle(env, "GET", "scenes", req("GET", "scenes")),
    );
    expect(list.scenes.find((s) => s.id === "beta-street")?.rejected).toBe(
      true,
    );
    // idempotent re-reject
    expect(
      (await handle(env, "POST", "scenes/beta-street/reject", req("POST", "x")))
        .status,
    ).toBe(200);
    expect(
      (
        await handle(
          env,
          "POST",
          "scenes/beta-street/restore",
          req("POST", "x"),
        )
      ).status,
    ).toBe(200);
    list = await json<SceneList>(
      await handle(env, "GET", "scenes", req("GET", "scenes")),
    );
    expect(list.scenes.find((s) => s.id === "beta-street")?.rejected).toBe(
      false,
    );
    expect(
      (await handle(env, "POST", "scenes/nope/reject", req("POST", "x")))
        .status,
    ).toBe(404);
  });

  test("comments trim + append + validation", async () => {
    const { env } = await makeEnv();
    expect(
      (
        await handle(
          env,
          "POST",
          "scenes/beta-street/comments",
          req("POST", "x", { text: "  hi  " }),
        )
      ).status,
    ).toBe(200);
    expect(
      (
        await handle(
          env,
          "POST",
          "scenes/beta-street/comments",
          req("POST", "x", { text: "" }),
        )
      ).status,
    ).toBe(400);
    expect(
      (
        await handle(
          env,
          "POST",
          "scenes/beta-street/comments",
          req("POST", "x", { text: "x".repeat(2001) }),
        )
      ).status,
    ).toBe(400);
    expect(
      (
        await handle(
          env,
          "POST",
          "scenes/nope/comments",
          req("POST", "x", { text: "hi" }),
        )
      ).status,
    ).toBe(404);
    const list = await json<SceneList>(
      await handle(env, "GET", "scenes", req("GET", "scenes")),
    );
    const beta = list.scenes.find((s) => s.id === "beta-street");
    expect(beta?.comments.length).toBe(1);
    expect(beta?.comments[0]?.text).toBe("hi");
  });
});

describe("legacy feedback migration (#1207)", () => {
  test("a pre-rename excluded map is read as rejections and rewritten", async () => {
    const { env, dir } = await makeEnv();
    await writeFile(
      path.join(dir, "feedback.json"),
      `${JSON.stringify({
        version: 1,
        accepted: { "alpha-market": "2026-09-01T00:00:00Z" },
        excluded: { "beta-street": "2026-09-02T00:00:00Z" },
        comments: {},
      })}\n`,
    );

    const list = await json<SceneList>(
      await handle(env, "GET", "scenes", req("GET", "scenes")),
    );
    const beta = list.scenes.find((s) => s.id === "beta-street");
    expect(beta?.rejected).toBe(true);
    expect(beta?.state).toBe("rejected");

    // Any write normalizes the file: the legacy key is gone, the rejection stays.
    expect(
      (
        await handle(
          env,
          "POST",
          "scenes/alpha-market/comments",
          req("POST", "x", { text: "x" }),
        )
      ).status,
    ).toBe(200);
    const stored = JSON.parse(
      await readFile(path.join(dir, "feedback.json"), "utf8"),
    );
    expect(stored.excluded).toBeUndefined();
    expect(stored.rejected["beta-street"]).toBeDefined();
  });
});
