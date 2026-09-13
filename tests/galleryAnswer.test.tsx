import { describe, expect, test } from "bun:test";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import {
  AnswerOverlay,
  answerCircleStyle,
  answerContainsPoint,
  isAnswerCircle,
} from "../src/gallery/answer";
import type { AnswerCircle } from "../src/gallery/api";
import { SceneModal } from "../src/gallery/SceneModal";
import { makeScene } from "./galleryFixtures";

const num = (v: string) => Number.parseFloat(v);

// A real scene's answer (the ticket's example): a tight circle low in the
// frame, the kind the overlay exists to make visible.
const ANSWER: AnswerCircle = { x: 0.185, y: 0.78, r: 0.045 };

describe("answer circle geometry (ticket #1209)", () => {
  test("draws the circle at the normalized center with radius r", () => {
    const s = answerCircleStyle(ANSWER);
    expect(s.left).toBe("14%");
    expect(s.top).toBe("73.5%");
    expect(s.width).toBe("9%");
    expect(s.height).toBe("9%");

    // what the browser draws, read back in normalized image coordinates
    const left = num(s.left) / 100;
    const top = num(s.top) / 100;
    const w = num(s.width) / 100;
    const h = num(s.height) / 100;
    expect(left + w / 2).toBeCloseTo(ANSWER.x, 5);
    expect(top + h / 2).toBeCloseTo(ANSWER.y, 5);
    expect(w / 2).toBeCloseTo(ANSWER.r, 5);
    expect(h / 2).toBeCloseTo(ANSWER.r, 5);
  });

  test("the drawn region matches the game's hit test", () => {
    // Mirror of the numbers the browser will position with, so the
    // assertion covers the same inputs the overlay uses (scoring.ts
    // clickDistance/isHit on the game side).
    const s = answerCircleStyle(ANSWER);
    const cx = (num(s.left) + num(s.width) / 2) / 100;
    const cy = (num(s.top) + num(s.height) / 2) / 100;
    const rx = num(s.width) / 2 / 100;
    const ry = num(s.height) / 2 / 100;

    // center: a hit
    expect(answerContainsPoint(ANSWER, cx, cy)).toBe(true);
    // just inside the drawn boundary on both axes: still a hit
    expect(answerContainsPoint(ANSWER, cx + rx * 0.99, cy)).toBe(true);
    expect(answerContainsPoint(ANSWER, cx, cy - ry * 0.99)).toBe(true);
    // just outside: a miss, or the overlay would mislead Evan
    expect(answerContainsPoint(ANSWER, cx + rx * 1.01, cy)).toBe(false);
    expect(answerContainsPoint(ANSWER, cx, cy + ry * 1.01)).toBe(false);
    // the boundary is a circle in normalized space: 0.99r on both axes is
    // inside, 1.01r is out
    const d = ANSWER.r * Math.SQRT1_2;
    expect(answerContainsPoint(ANSWER, cx + d * 0.99, cy + d * 0.99)).toBe(
      true,
    );
    expect(answerContainsPoint(ANSWER, cx + d * 1.01, cy + d * 1.01)).toBe(
      false,
    );
  });

  test("rejects malformed or missing answer data", () => {
    expect(isAnswerCircle(undefined)).toBe(false);
    expect(isAnswerCircle(null)).toBe(false);
    expect(isAnswerCircle({})).toBe(false);
    expect(isAnswerCircle({ x: 0.1, y: 0.2 })).toBe(false);
    expect(isAnswerCircle({ x: 0.1, y: 0.2, r: 0 })).toBe(false);
    expect(isAnswerCircle({ x: Number.NaN, y: 0.2, r: 0.1 })).toBe(false);
    expect(isAnswerCircle({ x: "0.1", y: 0.2, r: 0.1 })).toBe(false);
    expect(isAnswerCircle(ANSWER)).toBe(true);
  });

  test("AnswerOverlay renders nothing when hidden or without a valid answer", () => {
    const { container: hidden } = render(
      <AnswerOverlay answer={ANSWER} visible={false} />,
    );
    expect(hidden.querySelector(".td-answer-overlay")).toBeNull();

    const { container: missing } = render(
      <AnswerOverlay answer={undefined} visible={true} />,
    );
    expect(missing.querySelector(".td-answer-overlay")).toBeNull();
  });
});

/** The modal now hosts the generation-trace panel (#1373), which queries
 *  through react-query; render inside a client like the gallery does. */
function renderModal(scene: ReturnType<typeof makeScene>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SceneModal scene={scene} onClose={() => {}} />
    </QueryClientProvider>,
  );
}

describe("SceneModal answer overlay (ticket #1209)", () => {
  test("draws the click area on the scene tab by default, inside the image wrapper", () => {
    const scene = makeScene({ id: "f1", title: "Fresh Market" });
    renderModal(scene);

    const overlay = screen.getByTestId("td-answer-overlay");
    expect(overlay.style.left).toBe("35%");
    expect(overlay.style.top).toBe("55%");
    expect(overlay.style.width).toBe("10%");
    expect(overlay.style.height).toBe("10%");
    // positioned against the wrapper that shrink-wraps the rendered image,
    // not the <img> element box (which object-fit could letterbox)
    expect(overlay.parentElement?.className).toBe("td-img-wrap");
    expect(overlay.parentElement?.querySelector("img")).not.toBeNull();
  });

  test("toggle hides and shows the overlay and is keyboard-reachable", () => {
    const scene = makeScene({ id: "f1" });
    renderModal(scene);

    // the label wraps the checkbox, so the text is the accessible name
    const toggle = screen.getByLabelText("Click area") as HTMLInputElement;
    expect(toggle.type).toBe("checkbox");
    expect(toggle.checked).toBe(true);
    expect(toggle.disabled).toBe(false);
    toggle.focus();
    expect(document.activeElement).toBe(toggle);

    fireEvent.click(toggle);
    expect(screen.queryByTestId("td-answer-overlay")).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.getByTestId("td-answer-overlay")).toBeInTheDocument();
  });

  test("shows the overlay on the original tab, never on the audit crop", () => {
    const scene = makeScene({ id: "s1", audit: "yes" });
    renderModal(scene);

    fireEvent.click(screen.getByTestId("td-imgtab-original-s1"));
    expect(screen.getByTestId("td-answer-overlay")).toBeInTheDocument();

    // the audit crop is a zoomed detail: the normalized answer does not map
    // onto its frame, so the overlay and its toggle are disabled
    fireEvent.click(screen.getByTestId("td-imgtab-audit-s1"));
    expect(screen.queryByTestId("td-answer-overlay")).not.toBeInTheDocument();
    expect(
      (screen.getByLabelText("Click area") as HTMLInputElement).disabled,
    ).toBe(true);

    fireEvent.click(screen.getByTestId("td-imgtab-edited-s1"));
    expect(screen.getByTestId("td-answer-overlay")).toBeInTheDocument();
  });

  test("renders cleanly without answer data", () => {
    const scene = makeScene({ id: "s2", answer: null });
    renderModal(scene);

    expect(screen.queryByTestId("td-answer-overlay")).not.toBeInTheDocument();
    expect(screen.queryByText("Click area")).not.toBeInTheDocument();
    expect(screen.getByText("no answer data")).toBeInTheDocument();
  });
});
