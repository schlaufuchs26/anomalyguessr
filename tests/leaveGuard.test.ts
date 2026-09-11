/**
 * Unit tests for the deliberate-leave check (ticket #1230).
 *
 * The reload guard only stays quiet for a click that really replaces the
 * document. Every exclusion gets a test: marking a click that keeps the page
 * running would silently disarm the warning for the rest of the run, and a
 * missed navigation would pop the browser prompt at a player who asked for
 * that page.
 *
 * The events are built instead of dispatched: happy-dom follows a click on a
 * link and fetches its target (nothing listens on the CI runner), which
 * would fail the run for a reason that has nothing to do with this check.
 * The app-level click path is covered by the guard test on the gallery link
 * in `tests/app.test.tsx` and by the Playwright suite.
 */

import { describe, expect, test } from "bun:test";
import { isDeliberateNavigation } from "../src/leaveGuard";

/** The click the browser would deliver with `el` as its target. */
function clickOn(el: Element, init: MouseEventInit = {}): MouseEvent {
  const event = new MouseEvent("click", { button: 0, ...init });
  Object.defineProperty(event, "target", { value: el });
  return event;
}

/** An in-app link, as the gallery, mode (#1228) and frontpage (#1220) links. */
function anchor(attrs: Record<string, string> = {}): HTMLAnchorElement {
  const link = document.createElement("a");
  link.href = "/moderation";
  link.textContent = "Moderation";
  for (const [name, value] of Object.entries(attrs)) {
    link.setAttribute(name, value);
  }
  return link;
}

describe("isDeliberateNavigation (#1230)", () => {
  test("a plain click on an in-app link counts", () => {
    expect(isDeliberateNavigation(clickOn(anchor()))).toBe(true);
  });

  test("an explicit target=_self still counts", () => {
    expect(isDeliberateNavigation(clickOn(anchor({ target: "_self" })))).toBe(
      true,
    );
  });

  test("a click on a child of the link counts (the pointer lands there)", () => {
    const link = anchor();
    const label = document.createElement("span");
    label.textContent = "Moderation";
    link.append(label);
    expect(isDeliberateNavigation(clickOn(label))).toBe(true);
  });

  test("a new-tab link does not count: the page keeps running", () => {
    expect(isDeliberateNavigation(clickOn(anchor({ target: "_blank" })))).toBe(
      false,
    );
  });

  test("a download link does not count: the page keeps running", () => {
    expect(isDeliberateNavigation(clickOn(anchor({ download: "" })))).toBe(
      false,
    );
  });

  test("a modified click does not count: it opens a tab or window", () => {
    for (const modifier of ["ctrlKey", "metaKey", "shiftKey", "altKey"]) {
      expect(
        isDeliberateNavigation(clickOn(anchor(), { [modifier]: true })),
      ).toBe(false);
    }
  });

  test("a non-primary button does not count", () => {
    expect(isDeliberateNavigation(clickOn(anchor(), { button: 1 }))).toBe(
      false,
    );
    expect(isDeliberateNavigation(clickOn(anchor(), { button: 2 }))).toBe(
      false,
    );
  });

  test("a click that is not on a link does not count", () => {
    const button = document.createElement("button");
    button.textContent = "Next →";
    expect(isDeliberateNavigation(clickOn(button))).toBe(false);
  });
});
