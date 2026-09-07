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
