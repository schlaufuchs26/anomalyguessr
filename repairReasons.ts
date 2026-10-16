/**
 * The reasons the repair pass can act on (ticket #2658).
 *
 * `pipeline/ag_repair.py` repairs two classes: the click area (re-localize the
 * answer) and the text (regenerate the explanation). The moderation view uses
 * this set to enable "Needs work" only when every ticked reason is one the pass
 * would attempt; a single unrepairable reason makes the whole request
 * unrepairable, so the pass would only return the scene rejected.
 *
 * The Python side is the source of truth (`ag_repair.py` REPAIRABLE_REASONS,
 * itself CLICK_REASONS + TEXT_REASONS). A test reads those two tuples back and
 * compares, so a one-sided rename fails the suite. Every entry here is also a
 * canonical rejection reason (`rejectReasons.ts`).
 */
import type { RejectReason } from "./rejectReasons";

export const REPAIRABLE_REASONS = [
  "click area doesn't cover anomaly",
  "click area too big",
  "text is inaccurate",
] as const satisfies readonly RejectReason[];

/** Whether a reason is one the repair pass can act on. */
export function isRepairableReason(value: string): boolean {
  return (REPAIRABLE_REASONS as readonly string[]).includes(value);
}

/**
 * Whether a ticked reason set can go to the repair pass: at least one reason is
 * ticked and every ticked reason is repairable. One unrepairable reason makes
 * the whole request unrepairable, so "Needs work" stays off for it.
 */
export function canRequestRepair(reasons: readonly string[]): boolean {
  return reasons.length > 0 && reasons.every(isRepairableReason);
}
