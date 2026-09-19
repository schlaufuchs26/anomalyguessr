import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { isRejectReason, type RejectReason } from "../../rejectReasons.ts";
import {
  GenerateBusyError,
  type Generator,
  MANUAL_GENERATE_COUNT,
  MAX_GENERATE_COUNT,
} from "./generate.ts";
import { PENDING_REF_RE, pendingTracePath } from "./pending.ts";
import {
  auditCropPath,
  findScene,
  isScope,
  libraryImagePath,
  listScenes,
  liveDate,
  localDate,
  manifestScenes,
  sceneToApi,
  tracePath,
} from "./scenes.ts";
import type { Store } from "./store.ts";
import { MAX_COMMENT, type Moderation, type ModerationTag } from "./types.ts";

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

/** Whether a path segment names one of the two moderation tags (#1541). */
function isModerationTag(value: string | undefined): value is ModerationTag {
  return value === "funny" || value === "great";
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

/**
 * The canonical (long) id a scene reference names, or null when the reference
 * names nothing. Accepts the canonical id and the short handle ("AG-137",
 * ticket #1413), so every scene route resolves what a human typed.
 */
async function resolveSceneId(
  store: Store,
  ref: string,
): Promise<string | null> {
  const hit = findScene(await store.state(), ref);
  return hit === null ? null : hit.id;
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
    const hasTrace = (id: string): boolean => {
      try {
        return pathExists(tracePath(dataDir, id));
      } catch {
        return false;
      }
    };
    return json(listScenes(st, fb, hasAudit, hasTrace));
  }

  // GET /traces/{id}: the scene's generation trace sidecar (ticket #1373).
  // The trace is publishable text (no keys, no host paths) and served only
  // by file existence; the list payload stays small via hasTrace. `id` may
  // be the short handle (ticket #1413): the sidecar is keyed by the
  // canonical long id, so the handle resolves to it first.
  //
  // A ref that names no scene is tried as the in-flight trace's slug
  // (ticket #1446): the live view polls GET /generate for step metadata and
  // fetches the text of an opened step from here while the run is still
  // writing traces/pending/<slug>.json.
  if (method === "GET" && segs.length === 2 && segs[0] === "traces") {
    const ref = segs[1] ?? "";
    const id = await resolveSceneId(store, ref);
    const file =
      id !== null
        ? tracePath(dataDir, id)
        : PENDING_REF_RE.test(ref)
          ? pendingTracePath(dataDir, ref)
          : null;
    if (file === null) return notFound();
    let raw: string;
    try {
      raw = await readFile(file, "utf8");
    } catch {
      return notFound();
    }
    try {
      return json(JSON.parse(raw));
    } catch {
      return json({ error: "trace file is not valid JSON" }, 500);
    }
  }

  // GET /scenes/{id}: one scene by long id or short handle (ticket #1413),
  // the API side of the lookup. Same shape as one entry of GET /scenes.
  if (method === "GET" && segs.length === 2 && segs[0] === "scenes") {
    const st = await store.state();
    const hit = findScene(st, segs[1] ?? "");
    if (hit === null) return notFound();
    const fb = await store.feedback();
    return json(
      sceneToApi(
        hit.entry,
        fb,
        hit.id,
        (sid) => pathExists(auditCropPath(dataDir, sid)),
        (sid) => pathExists(tracePath(dataDir, sid)),
      ),
    );
  }

  // GET /scenes/{id}/image|original|audit
  if (method === "GET" && segs.length === 3 && segs[0] === "scenes") {
    const kind = segs[2] ?? "";
    if (kind !== "image" && kind !== "original" && kind !== "audit") {
      return notFound();
    }
    const id = await resolveSceneId(store, segs[1] ?? "");
    if (id === null) return notFound();
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
    const id = await resolveSceneId(store, segs[1] ?? "");
    if (id === null) return notFound();
    let body: { action?: string; feedback?: string; reason?: string };
    try {
      body = (await req.json()) as {
        action?: string;
        feedback?: string;
        reason?: string;
      };
    } catch {
      return badRequest("invalid JSON body");
    }
    const action = (body.action ?? "").trim();
    if (action !== "accept" && action !== "reject") {
      return badRequest("action must be one of: accept, reject");
    }
    const feedback = (body.feedback ?? "").trim();
    const reason = (body.reason ?? "").trim();
    // A canonical reason is stored structured (ticket #1627). Any other
    // string stays a plain comment, so an older client or a typo never
    // breaks a verdict.
    const canonical = action === "reject" && isRejectReason(reason);
    const comment = canonical
      ? feedback
      : [reason, feedback].filter(Boolean).join(": ");
    if (comment.length > MAX_COMMENT) {
      return badRequest("feedback must be at most 2000 characters");
    }
    const fb = await store.feedback();
    const now = new Date().toISOString();
    if (action === "accept") {
      delete fb.rejected[id];
      // An accepted scene carries no rejection reason any more.
      delete fb.reasons[id];
      if (!(id in fb.accepted)) fb.accepted[id] = now;
    } else {
      delete fb.accepted[id];
      if (!(id in fb.rejected)) fb.rejected[id] = now;
      if (canonical) {
        fb.reasons[id] = { reason: reason as RejectReason, at: now };
      }
    }
    if (comment !== "") {
      const comments = fb.comments[id] ?? [];
      comments.push({ text: comment, createdAt: now });
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
    const id = await resolveSceneId(store, segs[1] ?? "");
    if (id === null) return notFound();
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

  // POST /scenes/{id}/funny | /great  (the optional moderation tags,
  // #1502 and #1541). Same toggle semantics for both: a bodyless POST flips
  // the mark, `{tag:true|false}` sets it, and each tag is independent of the
  // accept/reject verdict and of the other tag.
  const tag = segs[2];
  if (
    method === "POST" &&
    segs.length === 3 &&
    segs[0] === "scenes" &&
    isModerationTag(tag)
  ) {
    const id = await resolveSceneId(store, segs[1] ?? "");
    if (id === null) return notFound();
    let body: { tag?: boolean };
    try {
      body = (await req.json()) as { tag?: boolean };
    } catch {
      body = {};
    }
    const fb = await store.feedback();
    const marks = fb[tag] ?? {};
    const next = typeof body.tag === "boolean" ? body.tag : !(id in marks);
    if (next) {
      if (!(id in marks)) marks[id] = new Date().toISOString();
    } else {
      delete marks[id];
    }
    fb[tag] = marks;
    await store.saveFeedback(fb);
    return json({ id, [tag]: next, [`${tag}At`]: marks[id] ?? null });
  }

  // POST /scenes/{id}/reject | /restore (and the moderate endpoint above)
  if (method === "POST" && segs.length === 3 && segs[0] === "scenes") {
    const action = segs[2] ?? "";
    if (action !== "reject" && action !== "restore") return notFound();
    const id = await resolveSceneId(store, segs[1] ?? "");
    if (id === null) return notFound();
    const fb = await store.feedback();
    if (action === "reject") {
      if (!(id in fb.rejected)) fb.rejected[id] = new Date().toISOString();
    } else {
      delete fb.rejected[id];
      delete fb.reasons[id];
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
