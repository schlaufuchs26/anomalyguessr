/**
 * The canonical rejection reasons (ticket #1627).
 *
 * The moderation view offers one button per reason; a click stores the exact
 * string below, not prose, so every rejection of the same kind reads the same
 * and the nightly feedback pass can count reasons.
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
  "scaling of anomaly is wrong",
  "anomaly doesn't make sense in context of image",
  "too easy",
] as const;

/** One canonical rejection reason. */
export type RejectReason = (typeof REJECT_REASONS)[number];

/** Whether a string is one of the five canonical rejection reasons. */
export function isRejectReason(value: string): value is RejectReason {
  return (REJECT_REASONS as readonly string[]).includes(value);
}
