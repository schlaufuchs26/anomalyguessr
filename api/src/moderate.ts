import { isRejectReason } from "../../rejectReasons.ts";
import { badRequest, json } from "./http.ts";
import { currentVerdict, type Store } from "./store.ts";
import {
  isRatingValue,
  MAX_COMMENT,
  type Moderation,
  type VerdictEntry,
} from "./types.ts";

/**
 * POST /scenes/{id}/moderate: the accept/reject/"Needs work" verdict, the
 * optional feedback text, the canonical reasons and the optional 0-10 rating
 * (ticket #2666) all land in one write to feedback.json. Split out of
 * `routes.ts` in ticket #2669, where the handler pushed the file past the
 * 500-line limit.
 *
 * `id` is already resolved to the canonical scene id by the caller.
 */
export async function moderateScene(
  store: Store,
  req: Request,
  id: string,
): Promise<Response> {
  let body: {
    action?: string;
    feedback?: string;
    reason?: string;
    reasons?: string[];
    rating?: unknown;
  };
  try {
    body = (await req.json()) as {
      action?: string;
      feedback?: string;
      reason?: string;
      reasons?: string[];
      rating?: unknown;
    };
  } catch {
    return badRequest("invalid JSON body");
  }
  const action = (body.action ?? "").trim();
  if (action !== "accept" && action !== "reject" && action !== "needs_work") {
    return badRequest("action must be one of: accept, reject, needs_work");
  }
  const feedback = (body.feedback ?? "").trim();
  // The moderation UI posts a list (ticket #2585); the legacy single
  // `reason` is still accepted, so an older client keeps working.
  const posted = [
    ...(typeof body.reason === "string" ? [body.reason] : []),
    ...(Array.isArray(body.reasons) ? body.reasons : []),
  ]
    .map((r) => (typeof r === "string" ? r.trim() : ""))
    .filter((r) => r !== "");
  // Canonical reasons are stored structured (ticket #1627), deduplicated
  // so a double post cannot double-count. Any other string stays a plain
  // comment, so an older client or a typo never breaks a verdict. An
  // accept carries none; a reject and a "Needs work" request keep them.
  const canonical =
    action === "accept" ? [] : [...new Set(posted.filter(isRejectReason))];
  const unknown = posted.filter((r) => !isRejectReason(r));
  const comment = [...unknown, feedback].filter(Boolean).join(": ");
  if (comment.length > MAX_COMMENT) {
    return badRequest("feedback must be at most 2000 characters");
  }
  // A "Needs work" request says what to fix, so it needs at least one
  // canonical reason; without one there is nothing for the repair pass to
  // act on (ticket #2590).
  if (action === "needs_work" && canonical.length === 0) {
    return badRequest("needs_work requires at least one reason");
  }
  // An optional rating rides with the verdict (#2666): the moderation view
  // keeps the slider local and sends the score here instead of one POST per
  // drag step. Absent means "leave the rating alone"; null clears it.
  const rawRating = body.rating;
  let rating: number | null | undefined;
  if (rawRating === undefined) rating = undefined;
  else if (rawRating === null) rating = null;
  else if (isRatingValue(rawRating)) rating = rawRating;
  else {
    return badRequest(
      "rating must be a whole number between 0 and 10, or null",
    );
  }
  const fb = await store.feedback();
  const now = new Date().toISOString();
  // The verdict this decision replaces (ticket #2606): a changed verdict
  // keeps the old one traceable, so the feedback pass can tell a
  // re-decision from a first judgement. `before` is read before the branch
  // below mutates the maps.
  const before = currentVerdict(fb, id);
  const remember = (next: VerdictEntry["verdict"]): void => {
    if (before !== null && before.verdict !== next) {
      fb.previous = fb.previous ?? {};
      fb.previous[id] = before;
    }
  };
  if (action === "accept") {
    delete fb.rejected[id];
    // An accepted scene carries no rejection reason any more.
    delete fb.reasons[id];
    // A human verdict supersedes an open repair request.
    if (fb.repairs?.[id]?.status === "requested") delete fb.repairs[id];
    if (!(id in fb.accepted)) {
      fb.accepted[id] = now;
      remember("accepted");
    }
  } else if (action === "reject") {
    delete fb.accepted[id];
    if (fb.repairs?.[id]?.status === "requested") delete fb.repairs[id];
    if (!(id in fb.rejected)) {
      fb.rejected[id] = now;
      remember("rejected");
    }
    if (canonical.length > 0) {
      fb.reasons[id] = { reason: canonical, at: now };
    }
  } else {
    // "Needs work": the scene leaves the accept/reject maps and the queue
    // (its moderation reads "repair"), carrying the ticked reasons for the
    // repair pass (ticket #2590). The verdicts stay untouched otherwise.
    delete fb.accepted[id];
    delete fb.rejected[id];
    delete fb.reasons[id];
    fb.repairs = fb.repairs ?? {};
    fb.repairs[id] = {
      status: "requested",
      reasons: canonical,
      requestedAt: now,
    };
  }
  if (comment !== "") {
    const comments = fb.comments[id] ?? [];
    comments.push({ text: comment, createdAt: now });
    fb.comments[id] = comments;
  }
  if (rating !== undefined) {
    fb.rating = fb.rating ?? {};
    if (rating === null) delete fb.rating[id];
    else fb.rating[id] = { value: rating, at: now };
  }
  await store.saveFeedback(fb);
  const moderation: Moderation =
    action === "accept"
      ? "accepted"
      : action === "reject"
        ? "rejected"
        : "repair";
  return json({
    id,
    moderation,
    ...(rating !== undefined ? { rating: rating ?? null } : {}),
    ...(action === "needs_work" ? { repair: fb.repairs?.[id] } : {}),
  });
}
