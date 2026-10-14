/**
 * The 0-10 quality rating control (ticket #2605), shared by the game's
 * moderation panel and the gallery (card + lightbox). It replaces the old
 * "great" tick: the rating is a human quality signal, independent of the
 * accept/reject verdict, and can be changed or cleared at any time.
 *
 * Eleven buttons (0..10) keep "unrated" distinct from a real 0; clicking the
 * active value clears the rating again.
 */

/** The rating scale's top value; a scene rated at least RATING_HIGH (8) is a
 *  positive example (kept in sync with api/src/types.ts). */
const RATING_MAX = 10;
export const RATING_HIGH = 8;

const VALUES = Array.from({ length: RATING_MAX + 1 }, (_, i) => i);

export interface RatingControlProps {
  /** The current rating, or null when unrated. */
  value: number | null;
  /** A post is in flight: the buttons are disabled. */
  busy?: boolean;
  /** Called with the new value, or null to clear. */
  onSelect: (value: number | null) => void;
  /** Accessible prefix; each button reads "<label> 8" (default "Rate"). */
  label?: string;
  /** Optional data-testid prefix: buttons get `<prefix>-<value>`. */
  testIdPrefix?: string;
  className?: string;
}

export function RatingControl({
  value,
  busy = false,
  onSelect,
  label = "Rate",
  testIdPrefix,
  className,
}: RatingControlProps) {
  return (
    <div className={className ? `rating ${className}` : "rating"}>
      <span className="rating-caption">Quality</span>
      {VALUES.map((v) => (
        <button
          key={v}
          type="button"
          className={`rating-btn${value === v ? " active" : ""}`}
          aria-pressed={value === v}
          aria-label={`${label} ${v}`}
          title={value === v ? "Click again to clear" : `Rate ${v} of 10`}
          disabled={busy}
          onClick={() => onSelect(value === v ? null : v)}
          data-testid={testIdPrefix ? `${testIdPrefix}-${v}` : undefined}
        >
          {v}
        </button>
      ))}
      <span className="rating-value" aria-live="polite">
        {value === null ? "not rated" : `${value}/${RATING_MAX}`}
      </span>
    </div>
  );
}
