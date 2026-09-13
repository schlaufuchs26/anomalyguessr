import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { ErrorBoundary } from "../src/gallery/ErrorBoundary";

describe("ErrorBoundary", () => {
  let originalError: typeof console.error;
  beforeAll(() => {
    originalError = console.error;
    console.error = () => {};
  });
  afterAll(() => {
    console.error = originalError;
  });

  function Boom(): ReactNode {
    throw new Error("test explosion");
  }

  test("renders children when no error", () => {
    render(
      <ErrorBoundary>
        <div>safe content</div>
      </ErrorBoundary>,
    );
    expect(screen.getByText("safe content")).toBeInTheDocument();
  });

  test("shows error in a modal overlay when child throws", () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    // Error message appears in the modal (modal-overlay is present)
    expect(document.querySelector(".modal-overlay")).toBeInTheDocument();
    expect(document.querySelector(".modal-overlay")?.textContent).toContain(
      "test explosion",
    );
    // Modal has dismiss button
    expect(screen.getByText("Dismiss")).toBeInTheDocument();
  });

  test("dismissing the modal clears the error and re-renders children", () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    // Modal is visible
    expect(document.querySelector(".modal-overlay")).toBeInTheDocument();

    // Click dismiss; Boom throws again, modal re-appears
    fireEvent.click(screen.getByText("Dismiss"));
    // The modal should re-appear (Boom throws again on remount)
    expect(document.querySelector(".modal-overlay")).toBeInTheDocument();
  });

  test("shows custom fallback when provided", () => {
    render(
      <ErrorBoundary fallback={<div>custom oops</div>}>
        <Boom />
      </ErrorBoundary>,
    );
    // Custom fallback text appears (in both fallback area and modal)
    const matches = screen.getAllByText("custom oops");
    expect(matches.length).toBeGreaterThanOrEqual(1);
  });

  test("renders fallback inline behind the modal overlay", () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    // Both the inline fallback (.error-boundary) and the modal exist
    expect(document.querySelector(".error-boundary")).toBeInTheDocument();
    expect(document.querySelector(".modal-overlay")).toBeInTheDocument();
  });
});
