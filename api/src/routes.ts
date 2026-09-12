import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import {
  GenerateBusyError,
  type Generator,
  MANUAL_GENERATE_COUNT,
  MAX_GENERATE_COUNT,
} from "./generate.ts";
import {
  auditCropPath,
  isScope,
  libraryImagePath,
  listScenes,
  liveDate,
  localDate,
  manifestScenes,
} from "./scenes.ts";
import { type Store, sceneExists } from "./store.ts";
import { MAX_COMMENT, type Moderation, SCENE_ID_RE } from "./types.ts";

export interface Env {
  store: Store;
  dataDir: string;
  /** On-demand generator control (GET/POST /generate). */
  generator: Generator;
}

function json(
  body: unknown,
  status = 200,
  extra?: Record<string, string>,
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...extra },
  });
}

function notFound(): Response {
  return json({ error: "not found" }, 404);
}

function badRequest(msg: string): Response {
  return json({ error: msg }, 400);
}

/** Handler shared by all routes; each returns a Response. */
export type Handler = (
  req: Request,
  env: Env,
  params: URLSearchParams,
  rest: string,
) => Promise<Response> | Response;

const IMAGE_TYPES: Record<"image" | "original" | "audit", string> = {
  image: "image/jpeg",
  original: "image/jpeg",
  audit: "image/png",
};

/** True when `id` is a safe slug naming a scene that exists in state.json. */
async function knownScene(store: Store, id: string): Promise<boolean> {
  if (!SCENE_ID_RE.test(id)) return false;
  return sceneExists(await store.state(), id);
}

/**
 * Route a request under /anomalyguessr/api. `rest` is the path after the
 * prefix, e.g. "manifest" or "scenes/foo/moderate".
 */
export async function handle(
  env: Env,
  method: string,
  rest: string,
  req: Request,
): Promise<Response> {
  const { store, dataDir } = env;
  const segs = rest.split("/").filter(Boolean);

  // GET /manifest?scope=...
  if (method === "GET" && segs.length === 1 && segs[0] === "manifest") {
    const url = new URL(req.url);
    const scope = url.searchParams.get("scope") ?? "all";
    if (!isScope(scope)) {
      return badRequest(
        "scope must be one of: all, unshown, shown, accepted, unmoderated, daily, live",
      );
    }
    const st = await store.state();
    const fb = await store.feedback();
    return json({
      version: 2,
      date: scope === "live" ? liveDate(st) : localDate(),
      scope,
      moderation: true,
      scenes: manifestScenes(st, fb, scope),
    });
  }

  // GET /generate: the on-demand generator's status
  if (method === "GET" && segs.length === 1 && segs[0] === "generate") {
    return json(await env.generator.status());
  }

  // POST /generate: start one generation batch
  if (method === "POST" && segs.length === 1 && segs[0] === "generate") {
    const text = (await req.text()).trim();
    let body: { count?: number } = {};
    if (text !== "") {
      try {
        body = JSON.parse(text) as { count?: number };
      } catch {
        return badRequest("invalid JSON body");
      }
    }
    // The body is optional (and count optional within it): 0 is "unset" and
    // means one daily set, like the Go API's nil body / zero int.
    const raw = body.count ?? 0;
    const count = raw === 0 ? MANUAL_GENERATE_COUNT : raw;
    if (!Number.isInteger(count) || count < 1 || count > MAX_GENERATE_COUNT) {
      return badRequest(`count must be between 1 and ${MAX_GENERATE_COUNT}`);
    }
    try {
      return json(await env.generator.start(count));
    } catch (err) {
      if (err instanceof GenerateBusyError) {
        return json({ error: err.message }, 409);
      }
      return json(
        {
          error: `start generation: ${err instanceof Error ? err.message : String(err)}`,
        },
        500,
      );
    }
  }

  // GET /scenes
  if (method === "GET" && segs.length === 1 && segs[0] === "scenes") {
    const st = await store.state();
    const fb = await store.feedback();
    const hasAudit = (id: string): boolean => {
      try {
        // Probe is only reached when a scene lacks an audit URL cache; the
        // data dir layout is trusted, so a stat is cheap and safe.
        return pathExists(auditCropPath(dataDir, id));
      } catch {
        return false;
      }
    };
    return json(listScenes(st, fb, hasAudit));
  }

  // GET /scenes/{id}/image|original|audit
  if (method === "GET" && segs.length === 3 && segs[0] === "scenes") {
    const id = segs[1] ?? "";
    const kind = segs[2] ?? "";
    if (kind !== "image" && kind !== "original" && kind !== "audit") {
      return notFound();
    }
    if (!(await knownScene(store, id))) return notFound();
    let p: string;
    if (kind === "audit") p = auditCropPath(dataDir, id);
    else p = libraryImagePath(dataDir, id, kind);
    try {
      const buf = await readFile(p);
      return new Response(buf, {
        status: 200,
        headers: { "Content-Type": IMAGE_TYPES[kind] },
      });
    } catch {
      return notFound();
    }
  }

  // POST /scenes/{id}/moderate
  if (
    method === "POST" &&
    segs.length === 3 &&
    segs[0] === "scenes" &&
    segs[2] === "moderate"
  ) {
    const id = segs[1] ?? "";
    if (!(await knownScene(store, id))) return notFound();
    let body: { action?: string; feedback?: string };
    try {
      body = (await req.json()) as { action?: string; feedback?: string };
    } catch {
      return badRequest("invalid JSON body");
    }
    const action = (body.action ?? "").trim();
    if (action !== "accept" && action !== "reject") {
      return badRequest("action must be one of: accept, reject");
    }
    const feedback = (body.feedback ?? "").trim();
    if (feedback.length > MAX_COMMENT) {
      return badRequest("feedback must be at most 2000 characters");
    }
    const fb = await store.feedback();
    const now = new Date().toISOString();
    if (action === "accept") {
      delete fb.rejected[id];
      if (!(id in fb.accepted)) fb.accepted[id] = now;
    } else {
      delete fb.accepted[id];
      if (!(id in fb.rejected)) fb.rejected[id] = now;
    }
    if (feedback !== "") {
      const comments = fb.comments[id] ?? [];
      comments.push({ text: feedback, createdAt: now });
      fb.comments[id] = comments;
    }
    await store.saveFeedback(fb);
    const moderation: Moderation =
      action === "accept" ? "accepted" : "rejected";
    return json({ id, moderation });
  }

  // POST /scenes/{id}/comments
  if (
    method === "POST" &&
    segs.length === 3 &&
    segs[0] === "scenes" &&
    segs[2] === "comments"
  ) {
    const id = segs[1] ?? "";
    if (!(await knownScene(store, id))) return notFound();
    let body: { text?: string };
    try {
      body = (await req.json()) as { text?: string };
    } catch {
      return badRequest("invalid JSON body");
    }
    const text = (body.text ?? "").trim();
    if (text === "") return badRequest("comment text is required");
    if (text.length > MAX_COMMENT) {
      return badRequest("comment text must be at most 2000 characters");
    }
    const fb = await store.feedback();
    const comment = { text, createdAt: new Date().toISOString() };
    const comments = fb.comments[id] ?? [];
    comments.push(comment);
    fb.comments[id] = comments;
    await store.saveFeedback(fb);
    return json({ comment });
  }

  // POST /scenes/{id}/reject | /restore (and the moderate endpoint above)
  if (method === "POST" && segs.length === 3 && segs[0] === "scenes") {
    const id = segs[1] ?? "";
    const action = segs[2] ?? "";
    if (action !== "reject" && action !== "restore") return notFound();
    if (!(await knownScene(store, id))) return notFound();
    const fb = await store.feedback();
    if (action === "reject") {
      if (!(id in fb.rejected)) fb.rejected[id] = new Date().toISOString();
    } else {
      delete fb.rejected[id];
    }
    await store.saveFeedback(fb);
    return json({ id, rejected: action === "reject" });
  }

  return notFound();
}

function pathExists(p: string): boolean {
  try {
    return existsSync(p);
  } catch {
    return false;
  }
}
