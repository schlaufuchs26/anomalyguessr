import { fitInto, panBy, type ViewState, zoomAt } from "../layout";

/** Normalized hit position fed to the app by the photo interactions. */
export interface PhotoHit {
  x: number;
  y: number;
}

/** One marker rendered on the overlay (percent coords from normalized ones). */
export interface PhotoMarker {
  type: "click" | "answer";
  x: number;
  y: number;
  /** Answer radius, normalized (answer marker only). */
  r?: number;
}

interface PhotoInteractionDeps {
  photoRef: () => HTMLDivElement | null;
  baseRef: () => HTMLImageElement | null;
  overlayRef: () => HTMLDivElement | null;
  view: ViewState;
  setView: (v: ViewState) => void;
}

/** Photo interactions: zoom, pan and click-to-guess on the image overlay. */
export interface PhotoInteractions {
  onOverlayClick: (
    e: { clientX: number; clientY: number },
    props: { answered: boolean; onGuess: (hit: PhotoHit) => void },
  ) => void;
  onDblClick: (
    e: { clientX: number; clientY: number; preventDefault: () => void },
    props: { answered: boolean; onUndoGuess: () => void },
  ) => void;
  onWheel: (e: {
    clientX: number;
    clientY: number;
    deltaY: number;
    preventDefault: () => void;
  }) => void;
  onPointerDown: (
    e: { clientX: number; clientY: number; pointerId: number },
    props: { answered: boolean },
  ) => void;
  onPointerMove: (e: {
    clientX: number;
    clientY: number;
    pointerId: number;
  }) => void;
  onPointerUp: (e: { pointerId: number }) => void;
  onPointerCancel: (e: { pointerId: number }) => void;
  fitPhotoToStage: () => void;
  resetScene: () => void;
  setCurrentView: (v: ViewState) => void;
}

/**
 * Imperative zoom/pan/click math for the photo overlay. The zoom view state
 * lives in React (PhotoStage); only the transform is written to the wrapper
 * element, because re-rendering the transform on every wheel tick would churn
 * the markup.
 *
 * The measurement contract: the image's layout box is re-measured when the
 * image loads or the viewport resizes (fitPhotoToStage); a missing
 * measurement is retried lazily on the next click so a guess can never be
 * dropped silently.
 */
export function createPhotoInteractions(
  deps: PhotoInteractionDeps,
): PhotoInteractions {
  let currentView = deps.view;
  let baseBox = { left: 0, top: 0, width: 0, height: 0 };
  let dragMoved = false;
  let dragStart: { x: number; y: number; view: ViewState } | null = null;
  const activePointers = new Map<number, { x: number; y: number }>();
  let pinchLast: { dist: number; midX: number; midY: number } | null = null;

  const DESKTOP_LAYOUT = "(min-width: 900px) and (min-height: 560px)";

  const setCurrentView = (v: ViewState): void => {
    currentView = v;
  };

  const applyTransform = (v: ViewState): void => {
    const photo = deps.photoRef();
    if (!photo) return;
    photo.style.transformOrigin = "0 0";
    photo.style.transform = `translate(${v.x}px, ${v.y}px) scale(${v.scale})`;
  };

  const measureBase = (): void => {
    const img = deps.baseRef();
    if (!img) return;
    const r = img.getBoundingClientRect();
    baseBox = { left: r.left, top: r.top, width: r.width, height: r.height };
  };

  const resetZoom = (): void => {
    const id = { scale: 1, x: 0, y: 0 };
    deps.setView(id);
    applyTransform(id);
  };

  /**
   * Fit the photo into the stage box on the laptop layout. The stage has a
   * fixed viewport-derived size there (CSS media query), so the image needs
   * explicit pixel dimensions to keep the click overlay aligned with the
   * displayed photo (a letterboxed object-fit would break the coordinate math
   * in onOverlayClick). On the stacked layout the stage is content-sized and
   * the plain CSS max-width constraint applies instead.
   */
  const fitPhotoToStage = (): void => {
    if (currentView.scale !== 1 || currentView.x !== 0 || currentView.y !== 0)
      resetZoom();
    const img = deps.baseRef();
    if (!img) return;
    // re-fit changes the layout box, which invalidates the cached base
    img.style.width = "";
    img.style.height = "";
    if (typeof window.matchMedia !== "function") {
      measureBase();
      return;
    }
    if (!window.matchMedia(DESKTOP_LAYOUT).matches) {
      if (img.naturalWidth) measureBase();
      return;
    }
    if (!img.naturalWidth) return;
    const stage = deps.photoRef()?.parentElement;
    const w = stage?.clientWidth ?? 0;
    const h = stage?.clientHeight ?? 0;
    if (w <= 0 || h <= 0) return;
    // keep the 1px photo border inside the stage
    const fit = fitInto(w - 2, h - 2, img.naturalWidth, img.naturalHeight);
    if (!fit) return;
    img.style.width = `${fit.width}px`;
    img.style.height = `${fit.height}px`;
    measureBase();
  };

  /** Reset per-scene interaction state (view, drag, pointers, sizing). */
  const resetScene = (): void => {
    const id = { scale: 1, x: 0, y: 0 };
    deps.setView(id);
    applyTransform(id);
    dragMoved = false;
    dragStart = null;
    activePointers.clear();
    pinchLast = null;
    const img = deps.baseRef();
    if (img) {
      img.style.width = "";
      img.style.height = "";
    }
    fitPhotoToStage();
  };

  const zoomAtPoint = (
    cursorX: number,
    cursorY: number,
    factor: number,
  ): void => {
    if (baseBox.width === 0) return;
    const next = zoomAt(
      currentView,
      baseBox.width,
      baseBox.height,
      cursorX - baseBox.left,
      cursorY - baseBox.top,
      factor,
    );
    deps.setView(next);
    applyTransform(next);
  };

  const onOverlayClick = (
    e: { clientX: number; clientY: number },
    props: { answered: boolean; onGuess: (hit: PhotoHit) => void },
  ): void => {
    if (props.answered) return;
    if (dragMoved) {
      // the gesture was a pan, not a guess
      dragMoved = false;
      return;
    }
    if (baseBox.width === 0 || baseBox.height === 0) {
      measureBase(); // image loaded before the listener was attached
      if (baseBox.width === 0 || baseBox.height === 0) return;
    }
    // view-space cursor -> image-local -> normalized (the guess stays in
    // original image coordinates; only the display is zoomed/panned)
    const px = e.clientX - baseBox.left;
    const py = e.clientY - baseBox.top;
    const nx = (px - currentView.x) / currentView.scale / baseBox.width;
    const ny = (py - currentView.y) / currentView.scale / baseBox.height;
    if (nx < 0 || nx > 1 || ny < 0 || ny > 1) return;
    props.onGuess({ x: nx, y: ny });
  };

  const onDblClick = (
    e: { clientX: number; clientY: number; preventDefault: () => void },
    props: { answered: boolean; onUndoGuess: () => void },
  ): void => {
    e.preventDefault();
    if (currentView.scale > 1.001) {
      resetZoom();
      return;
    }
    // a double-click means "zoom in here", not "guess twice": undo the
    // first click's guess resolution, then zoom
    if (props.answered) props.onUndoGuess();
    zoomAtPoint(e.clientX, e.clientY, 3);
  };

  const onWheel = (e: {
    clientX: number;
    clientY: number;
    deltaY: number;
    preventDefault: () => void;
  }): void => {
    e.preventDefault();
    zoomAtPoint(e.clientX, e.clientY, Math.exp(-e.deltaY * 0.0016));
  };

  const pointerPos = (e: { clientX: number; clientY: number }) => ({
    x: e.clientX,
    y: e.clientY,
  });

  const onPointerDown = (
    e: { clientX: number; clientY: number; pointerId: number },
    props: { answered: boolean },
  ): void => {
    if (props.answered) return;
    activePointers.set(e.pointerId, pointerPos(e));
    if (activePointers.size === 1) {
      dragStart = { x: e.clientX, y: e.clientY, view: { ...currentView } };
      dragMoved = false;
      try {
        deps.overlayRef()?.setPointerCapture(e.pointerId);
      } catch {
        // pointer capture is best-effort
      }
    } else if (activePointers.size === 2) {
      dragStart = null;
      const [a, b] = [...activePointers.values()];
      if (a && b) {
        pinchLast = {
          dist: Math.hypot(a.x - b.x, a.y - b.y),
          midX: (a.x + b.x) / 2,
          midY: (a.y + b.y) / 2,
        };
      }
    }
  };

  const onPointerMove = (e: {
    clientX: number;
    clientY: number;
    pointerId: number;
  }): void => {
    if (!activePointers.has(e.pointerId)) return;
    activePointers.set(e.pointerId, pointerPos(e));
    const pts = [...activePointers.values()];
    if (pts.length === 2 && pinchLast) {
      const [a, b] = pts;
      if (!a || !b) return;
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      const midX = (a.x + b.x) / 2;
      const midY = (a.y + b.y) / 2;
      if (pinchLast.dist > 0) {
        let next = zoomAt(
          currentView,
          baseBox.width,
          baseBox.height,
          pinchLast.midX - baseBox.left,
          pinchLast.midY - baseBox.top,
          dist / pinchLast.dist,
        );
        // follow the pinch midpoint's movement
        next = panBy(
          next,
          midX - pinchLast.midX,
          midY - pinchLast.midY,
          baseBox.width,
          baseBox.height,
        );
        deps.setView(next);
        applyTransform(next);
      }
      pinchLast = { dist, midX, midY };
      return;
    }
    if (dragStart && pts.length === 1) {
      const dx = e.clientX - dragStart.x;
      const dy = e.clientY - dragStart.y;
      if (Math.hypot(dx, dy) > 4) dragMoved = true;
      if (currentView.scale > 1 || dragMoved) {
        const next = panBy(
          dragStart.view,
          dx,
          dy,
          baseBox.width,
          baseBox.height,
        );
        deps.setView(next);
        applyTransform(next);
      }
    }
  };

  const endPointer = (pointerId: number): void => {
    activePointers.delete(pointerId);
    pinchLast = null;
    if (activePointers.size === 0) dragStart = null;
  };

  return {
    onOverlayClick,
    onDblClick,
    onWheel,
    onPointerDown,
    onPointerMove,
    onPointerUp: (e) => endPointer(e.pointerId),
    onPointerCancel: (e) => endPointer(e.pointerId),
    fitPhotoToStage,
    resetScene,
    setCurrentView,
  };
}
