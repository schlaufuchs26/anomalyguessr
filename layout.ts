export interface FittedSize {
  width: number;
  height: number;
}

/**
 * Downscale natural image dimensions so the image fits inside a box of the
 * given size, preserving the aspect ratio. Never upscales: when the box is
 * larger than the natural size, the natural size is returned unchanged.
 * Returns null when any dimension is non-positive.
 */
export function fitInto(
  boxWidth: number,
  boxHeight: number,
  naturalWidth: number,
  naturalHeight: number,
): FittedSize | null {
  if (
    boxWidth <= 0 ||
    boxHeight <= 0 ||
    naturalWidth <= 0 ||
    naturalHeight <= 0
  ) {
    return null;
  }
  const scale = Math.min(boxWidth / naturalWidth, boxHeight / naturalHeight, 1);
  return {
    width: Math.round(naturalWidth * scale),
    height: Math.round(naturalHeight * scale),
  };
}

/** Zoom/pan view state: scale + pixel offset (transform-origin top-left). */
export interface ViewState {
  scale: number;
  x: number;
  y: number;
}

export const VIEW_MIN_SCALE = 1;
export const VIEW_MAX_SCALE = 8;

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

/**
 * Keep the pan offset valid for a scale: the scaled photo (w*scale wide,
 * anchored at x/y, origin top-left) must still cover the view window so the
 * player cannot pan the image fully out of sight.
 */
export function clampView(
  x: number,
  y: number,
  scale: number,
  viewWidth: number,
  viewHeight: number,
): ViewState {
  const s = clamp(scale, VIEW_MIN_SCALE, VIEW_MAX_SCALE);
  const minX = Math.min(0, viewWidth - viewWidth * s);
  const minY = Math.min(0, viewHeight - viewHeight * s);
  return { scale: s, x: clamp(x, minX, 0), y: clamp(y, minY, 0) };
}

/**
 * Zoom by `factor` keeping the image point under `cursor` (view-window
 * coordinates) fixed, then clamp the pan. factor > 1 zooms in.
 */
export function zoomAt(
  view: ViewState,
  viewWidth: number,
  viewHeight: number,
  cursorX: number,
  cursorY: number,
  factor: number,
): ViewState {
  const scale = clamp(view.scale * factor, VIEW_MIN_SCALE, VIEW_MAX_SCALE);
  // image-space point currently under the cursor
  const ix = (cursorX - view.x) / view.scale;
  const iy = (cursorY - view.y) / view.scale;
  return clampView(
    cursorX - ix * scale,
    cursorY - iy * scale,
    scale,
    viewWidth,
    viewHeight,
  );
}

/** Pan by a pixel delta (view-window space), clamped. */
export function panBy(
  view: ViewState,
  dx: number,
  dy: number,
  viewWidth: number,
  viewHeight: number,
): ViewState {
  return clampView(view.x + dx, view.y + dy, view.scale, viewWidth, viewHeight);
}
