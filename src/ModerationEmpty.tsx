/**
 * Empty state of the dev moderation queue (tickets #1202, #1210, #1287,
 * #1584).
 *
 * #1202 made an empty queue a calm message instead of a broken game shell.
 * #1210 added the pipeline buffer and the on-demand generation button to this
 * state; #1584 moved that control into its own component (GenerateControl) so
 * the populated moderation view can show it too. The calm copy stays here:
 * only the empty state says the queue is empty.
 */
import { DEFAULT_GENERATE_POLL_MS, GenerateControl } from "./GenerateControl";

export function ModerationEmpty({
  onReload,
  pollMs = DEFAULT_GENERATE_POLL_MS,
}: {
  onReload: () => void;
  /** Poll cadence while a run is in flight; tests drive a shorter one. */
  pollMs?: number;
}) {
  return (
    <section id="empty" className="empty-state">
      <h2>Moderation queue is empty</h2>
      <p>
        Nothing to review right now. New scenes appear after the daily
        generation run.
      </p>
      <GenerateControl
        onFinished={onReload}
        onReload={onReload}
        pollMs={pollMs}
      />
    </section>
  );
}
