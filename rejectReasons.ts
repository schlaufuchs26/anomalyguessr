/**
 * The canonical rejection reasons (ticket #1627).
 *
 * The moderation view offers one toggle per reason; a submit stores the exact
 * strings below, not prose, so every rejection of the same kind reads the
 * same and the nightly feedback pass can count reasons. Since ticket #2585
 * several reasons can be ticked at once, so the stored decision carries a
 * list; a row written before #2585 carries a single string.
 *
 * The list lives at the repo root because both packages need it: the frontend
 * bundles it, and the `api/` service imports it to validate a posted reason.
 * The Python side mirrors it where it reads the feedback file
 * (`pipeline/ag_queue.py` REJECT_REASONS); an unknown string is never an
 * error, the API keeps it as a plain comment.
 */
export const REJECT_REASONS = [
  "doesn't match style of image",
  "click area doesn't cover anomaly",
  "click area too big",
  "scaling of anomaly is wrong",
  "anomaly doesn't make sense in context of image",
  "too easy",
  "too hard",
  "text is inaccurate",
] as const;

/** One canonical rejection reason. */
export type RejectReason = (typeof REJECT_REASONS)[number];

/** Whether a string is one of the canonical rejection reasons. */
export function isRejectReason(value: string): value is RejectReason {
  return (REJECT_REASONS as readonly string[]).includes(value);
}

/**
 * The reasons of a stored feedback entry as a list (ticket #2585).
 *
 * A row written before #2585 stores one string under `reason`; from #2585 on
 * it stores a list. Every reader goes through here, so an old row reads as a
 * one-element list without rewriting history.
 */
export function entryReasons(entry: unknown): RejectReason[] {
  const raw = (entry as { reason?: unknown } | null | undefined)?.reason;
  const list = Array.isArray(raw) ? raw : raw == null ? [] : [raw];
  return list.filter(
    (r): r is RejectReason => typeof r === "string" && isRejectReason(r),
  );
}
