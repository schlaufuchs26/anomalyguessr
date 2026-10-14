/**
 * The 0-10 quality rating control (ticket #2605), shared by the game's
 * moderation panel and the gallery (card + lightbox). It replaces the old
 * "great" tick: the rating is a human quality signal, independent of the
 * accept/reject verdict, and can be changed or cleared at any time.
 *
 * A range slider (0-10, integer steps) with the number next to it; the native
 * control handles the arrow keys. "Unrated" stays distinct from a real 0: an
 * unrated scene shows the knob at 0 but the label reads "not rated", and the
 * Clear button (disabled while unrated) returns it to unset.
 */

/** The rating scale's top value; a scene rated at least RATING_HIGH (8) is a
 *  positive example (kept in sync with api/src/types.ts). */
const RATING_MAX = 10;
export const RATING_HIGH = 8;

export interface RatingControlProps {
  /** The current rating, or null when unrated. */
  value: number | null;
  /** A post is in flight: the control is disabled. */
  busy?: boolean;
  /** Called with the new value, or null to clear. */
  onSelect: (value: number | null) => void;
  /** Accessible name of the slider (default "Quality rating"). */
  label?: string;
  /** Optional data-testid for the slider; the clear button gets `<prefix>-clear`. */
  testIdPrefix?: string;
  className?: string;
}

export function RatingControl({
  value,
  busy = false,
  onSelect,
  label = "Quality rating",
  testIdPrefix,
  className,
}: RatingControlProps) {
  const unrated = value === null;
  return (
    <div className={className ? `rating ${className}` : "rating"}>
      <span className="rating-caption">Quality</span>
      <input
        type="range"
        className={`rating-slider${unrated ? " unset" : ""}`}
        min={0}
        max={RATING_MAX}
        step={1}
        value={value ?? 0}
        disabled={busy}
        aria-label={label}
        aria-valuetext={unrated ? "not rated" : `${value} of ${RATING_MAX}`}
        onChange={(e) => onSelect(Number(e.target.value))}
        data-testid={testIdPrefix}
      />
      <span className="rating-value" aria-live="polite">
        {unrated ? "not rated" : `${value}/${RATING_MAX}`}
      </span>
      <button
        type="button"
        className="rating-clear"
        disabled={busy || unrated}
        title="Clear the rating"
        onClick={() => onSelect(null)}
        data-testid={testIdPrefix ? `${testIdPrefix}-clear` : undefined}
      >
        clear
      </button>
    </div>
  );
}
