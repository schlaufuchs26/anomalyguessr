import { useMutation, useQueryClient } from "@tanstack/react-query";
import { RATING_HIGH, RatingControl } from "../RatingControl";
import { agUrl, QUERY_KEY, type TDScene } from "./api";

/**
 * The "funny" tag (ticket #1502) and the 0-10 quality rating (ticket #2605,
 * replacing the "great" tick), as true toggles in the gallery.
 *
 * The gallery is visited after the decision, so unlike the game panel (where
 * a tag is one-way) a wrong mark must be undoable here: each control posts
 * the state it is moving to and has its own pending/error state. Shared by
 * the card and the lightbox so both behave the same.
 */

/** The tag in display order, with its checkbox label and chip title. */
export const TAGS = [
  {
    tag: "funny",
    label: "😄 Funny",
    chip: "😄 funny",
    title: "Marked as deliberately funny (ticket #1502)",
  },
] as const;

export type TagName = (typeof TAGS)[number]["tag"];

/** The read-only chip that shows a set tag on the scene card. */
export function TagChip({
  scene,
  tag,
  chip,
  title,
}: {
  scene: TDScene;
  tag: TagName;
  chip: string;
  title: string;
}) {
  const on = tag === "funny" ? scene.funny : false;
  if (!on) return null;
  return (
    <span
      className={`td-chip td-chip-${tag}`}
      title={title}
      data-testid={`td-${tag}-${scene.id}`}
    >
      {chip}
    </span>
  );
}

/** The read-only rating chip on the scene card (ticket #2605). */
export function RatingChip({ scene }: { scene: TDScene }) {
  if (scene.rating === null || scene.rating === undefined) return null;
  const high = scene.rating >= RATING_HIGH;
  return (
    <span
      className={`td-chip td-chip-rating${high ? " high" : ""}`}
      title={
        high
          ? "Rated 8+ out of 10: a positive example (ticket #2605)"
          : "Quality rating (ticket #2605)"
      }
      data-testid={`td-rating-${scene.id}`}
    >
      {high ? "★" : "☆"} {scene.rating}/10
    </span>
  );
}

/** The "funny" checkbox with its own mutation, pending and error state. */
function TagCheckbox({
  scene,
  tag,
  label,
  title,
}: {
  scene: TDScene;
  tag: TagName;
  label: string;
  title: string;
}) {
  const queryClient = useQueryClient();
  const checked = scene.funny;
  const mark = useMutation({
    mutationFn: async (value: boolean) => {
      const res = await fetch(agUrl(`scenes/${scene.id}/${tag}`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: value }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json() as Promise<{ id: string }>;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });
  return (
    <label
      className={`td-tag${checked ? " active" : ""}`}
      title={title}
      data-testid={`td-${tag}-label-${scene.id}`}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={mark.isPending}
        onChange={(e) => mark.mutate(e.target.checked)}
        data-testid={`td-${tag}-toggle-${scene.id}`}
      />
      {mark.isPending ? "…" : label}
      {mark.error ? (
        <span
          className="td-tag-error"
          data-testid={`td-${tag}-error-${scene.id}`}
        >
          {mark.error.message}
        </span>
      ) : null}
    </label>
  );
}

/** The funny tag and the rating control for a scene; the caller places them. */
export function TagToggles({
  scene,
  className,
}: {
  scene: TDScene;
  className?: string;
}) {
  const queryClient = useQueryClient();
  const rate = useMutation({
    mutationFn: async (value: number | null) => {
      const res = await fetch(agUrl(`scenes/${scene.id}/rating`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rating: value }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json() as Promise<{ id: string }>;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });
  return (
    <div className={className ? `td-tags ${className}` : "td-tags"}>
      {TAGS.map((t) => (
        <TagCheckbox
          key={t.tag}
          scene={scene}
          tag={t.tag}
          label={t.label}
          title={t.title}
        />
      ))}
      <RatingControl
        value={scene.rating ?? null}
        busy={rate.isPending}
        onSelect={(v) => rate.mutate(v)}
        label="Rate"
        testIdPrefix={`td-rating-${scene.id}`}
      />
      {rate.error ? (
        <span
          className="td-tag-error"
          data-testid={`td-rating-error-${scene.id}`}
        >
          {rate.error.message}
        </span>
      ) : null}
    </div>
  );
}
