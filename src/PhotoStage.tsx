import { useEffect, useRef, useState } from "react";
import type { ViewState } from "../layout";
import {
  createPhotoInteractions,
  type PhotoHit,
  type PhotoMarker,
} from "./photoInteractions";

export interface PhotoStageProps {
  /** Alt text for the edited photo (scene title, place, year). */
  alt: string;
  /** Edited image URL (base layer). */
  image: string;
  /** Untouched original URL (reveal layer). */
  original: string;
  /** A guess is resolved: the overlay stops accepting clicks. */
  answered: boolean;
  /** Original layer visible via the manual compare toggle (`.show`). */
  showOriginal: boolean;
  /** Correct-guess crossfade + glitch animation (`.reveal`, #1147). */
  correcting: boolean;
  /** Markers for the resolved guess (click position + answer circle). */
  markers: PhotoMarker[];
  onGuess: (hit: PhotoHit) => void;
  /** Double-click on an answered photo: undo the guess, then zoom in. */
  onUndoGuess: () => void;
  onOriginalLoaded: () => void;
  onOriginalError: () => void;
}

/**
 * The clickable photo: the edited image as the base layer, the untouched
 * original as an absolutely positioned layer above it (preloaded per scene,
 * so a correct guess needs no network wait). Zoom/pan/click math lives in
 * photoInteractions; this component renders the layers and wires the events.
 */
export function PhotoStage(props: PhotoStageProps) {
  const photoRef = useRef<HTMLDivElement>(null);
  const baseRef = useRef<HTMLImageElement>(null);
  const origRef = useRef<HTMLImageElement>(null);
  const overlayRef = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<ViewState>({ scale: 1, x: 0, y: 0 });

  const ix = useRef(
    createPhotoInteractions({
      photoRef: () => photoRef.current,
      baseRef: () => baseRef.current,
      overlayRef: () => overlayRef.current,
      view,
      setView,
    }),
  ).current;
  ix.setCurrentView(view);

  // The stage has a viewport-derived size on the laptop layout, so a resize
  // (or a crossing of the layout breakpoint) needs a re-fit to keep the click
  // overlay aligned with the displayed photo.
  useEffect(() => {
    const refit = () => ix.fitPhotoToStage();
    window.addEventListener("resize", refit);
    const mq =
      typeof window.matchMedia === "function"
        ? window.matchMedia("(min-width: 900px) and (min-height: 560px)")
        : null;
    mq?.addEventListener("change", refit);
    return () => {
      window.removeEventListener("resize", refit);
      mq?.removeEventListener("change", refit);
    };
  }, [ix]);

  // Wheel-zoom needs a non-passive listener (React's onWheel is passive, so
  // preventDefault there would be ignored and the page would scroll along
  // with the zoom); the vanilla game attached it non-passive as well.
  useEffect(() => {
    const overlay = overlayRef.current;
    if (!overlay) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      ix.onWheel({
        clientX: e.clientX,
        clientY: e.clientY,
        deltaY: e.deltaY,
        preventDefault: () => e.preventDefault(),
      });
    };
    overlay.addEventListener("wheel", onWheel, { passive: false });
    return () => overlay.removeEventListener("wheel", onWheel);
  }, [ix]);

  return (
    <div className="stage">
      <div
        ref={photoRef}
        id="photo"
        className={`photo${props.correcting ? " correcting" : ""}`}
      >
        <img
          ref={baseRef}
          id="photo-img"
          src={props.image}
          alt={props.alt}
          onLoad={() => ix.fitPhotoToStage()}
        />
        <img
          ref={origRef}
          id="photo-orig"
          className={`photo-orig${props.showOriginal ? " show" : ""}${
            props.correcting ? " reveal" : ""
          }`}
          src={props.original}
          alt=""
          aria-hidden="true"
          onLoad={props.onOriginalLoaded}
          onError={props.onOriginalError}
        />
        {/* biome-ignore lint/a11y/useKeyWithClickEvents: the photo is a click map
            (as in the vanilla game); a keyboard cursor on an image would be a
            new feature, not a port. Hints and navigation stay real buttons. */}
        <div
          ref={overlayRef}
          id="overlay"
          role="application"
          aria-label="Photo: click the spot where something does not belong"
          className={`overlay${props.answered ? " waiting" : ""}`}
          onClick={(e) =>
            ix.onOverlayClick(
              { clientX: e.clientX, clientY: e.clientY },
              { answered: props.answered, onGuess: props.onGuess },
            )
          }
          onDoubleClick={(e) =>
            ix.onDblClick(
              {
                clientX: e.clientX,
                clientY: e.clientY,
                preventDefault: () => e.preventDefault(),
              },
              { answered: props.answered, onUndoGuess: props.onUndoGuess },
            )
          }
          onPointerDown={(e) =>
            ix.onPointerDown(
              {
                clientX: e.clientX,
                clientY: e.clientY,
                pointerId: e.pointerId,
              },
              { answered: props.answered },
            )
          }
          onPointerMove={(e) =>
            ix.onPointerMove({
              clientX: e.clientX,
              clientY: e.clientY,
              pointerId: e.pointerId,
            })
          }
          onPointerUp={(e) => ix.onPointerUp({ pointerId: e.pointerId })}
          onPointerCancel={(e) =>
            ix.onPointerCancel({ pointerId: e.pointerId })
          }
        >
          {props.markers.map((m) => (
            <div
              key={`${m.type}-${m.x}-${m.y}`}
              className={`marker ${m.type}`}
              style={{
                left: `${m.x * 100}%`,
                top: `${m.y * 100}%`,
                ...(m.r
                  ? { width: `${m.r * 200}%`, height: `${m.r * 200}%` }
                  : {}),
              }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
