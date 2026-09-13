import React from "react";

/** Where the chip is rendered. The scene detail view shows more than one
 *  chip at a time (card behind + modal header + image view + trace panel),
 *  so each placement gets its own test hook (ticket #1433). */
export type SceneHandlePlacement = "card" | "modal" | "view" | "trace";

/**
 * A scene's short handle ("AG-137", ticket #1413) as a click-to-copy chip.
 * The long id stays the canonical key for files, URLs and trace sidecars;
 * the handle is what one says out loud, so a click copies exactly that. A
 * browser that refuses the clipboard (no API, insecure context) leaves the
 * chip unchanged instead of pretending the copy worked.
 *
 * Legibility (ticket #1433): the chip carries its own solid background and a
 * visible border instead of the old `opacity: 0.75` + `currentColor` border,
 * which left the inherited button colour (black) on the dark page. Contrast
 * values are pinned in tests/galleryStyle.test.ts.
 */
export function SceneHandle({
  shortId,
  id,
  placement,
}: {
  /** The handle; absent on a legacy entry the backfill has not reached. */
  shortId?: string | undefined;
  /** The canonical scene id, for the tooltip + test hook. */
  id: string;
  placement: SceneHandlePlacement;
}) {
  const [copied, setCopied] = React.useState(false);
  if (!shortId) return null;

  const copy = async () => {
    const clip = navigator.clipboard;
    if (!clip?.writeText) return;
    try {
      await clip.writeText(shortId);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      // Clipboard refused: the handle stays readable, nothing to report.
    }
  };

  return (
    <button
      type="button"
      className={`td-handle${copied ? " copied" : ""}`}
      onClick={() => void copy()}
      title={`Copy ${shortId} (scene id: ${id})`}
      aria-label={`Copy short id ${shortId}`}
      data-testid={`td-handle-${placement}-${id}`}
    >
      {copied ? `${shortId} ✓` : shortId}
    </button>
  );
}
