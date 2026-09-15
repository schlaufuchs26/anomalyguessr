import { useMutation, useQueryClient } from "@tanstack/react-query";
import { agUrl, QUERY_KEY, type TDScene } from "./api";

/**
 * The two optional moderation tags (tickets #1502, #1541) as true toggles.
 *
 * The gallery is visited after the decision, so unlike the game panel (where
 * a tag is one-way) a wrong tick must be undoable here: each checkbox posts
 * the state it is moving to and its own pending/error state. Shared by the
 * card and the lightbox so both behave the same.
 */

/** The tags in display order, with their checkbox label and chip title. */
export const TAGS = [
  {
    tag: "funny",
    label: "😄 Funny",
    chip: "😄 funny",
    title: "Marked as deliberately funny (ticket #1502)",
  },
  {
    tag: "great",
    label: "⭐ Great",
    chip: "⭐ great",
    title: "Marked as a positive example (ticket #1541)",
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
  const on = tag === "funny" ? scene.funny : scene.great;
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

/** One checkbox tag toggle with its own mutation, pending and error state. */
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
  const checked = tag === "funny" ? scene.funny : scene.great;
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

/** Both tag checkboxes for a scene; `className` lets the caller place them. */
export function TagToggles({
  scene,
  className,
}: {
  scene: TDScene;
  className?: string;
}) {
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
    </div>
  );
}
