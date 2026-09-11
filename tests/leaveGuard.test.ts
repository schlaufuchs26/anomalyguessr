/**
 * Unit tests for the deliberate-leave check (ticket #1230).
 *
 * The reload guard only stays quiet for a click that really replaces the
 * document. Every exclusion gets a test: marking a click that keeps the page
 * running would silently disarm the warning for the rest of the run, and a
 * missed navigation would pop the browser prompt at a player who asked for
 * that page.
 */

import { describe, expect, test } from "bun:test";
import { isDeliberateNavigation } from "../src/leaveGuard";

/** A click event as the browser would deliver it on the clicked element. */
function dispatchOn(el: Element, init: MouseEventInit = {}): MouseEvent {
  const event = new MouseEvent("click", { button: 0, ...init });
  el.dispatchEvent(event);
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
  document.body.append(link);
  return link;
}

describe("isDeliberateNavigation (#1230)", () => {
  test("a plain click on an in-app link counts", () => {
    expect(isDeliberateNavigation(dispatchOn(anchor()))).toBe(true);
  });

  test("an explicit target=_self still counts", () => {
    expect(
      isDeliberateNavigation(dispatchOn(anchor({ target: "_self" }))),
    ).toBe(true);
  });

  test("a click on a child of the link counts (the click lands there)", () => {
    const link = anchor();
    const span = document.createElement("span");
    span.textContent = "Mode name";
    link.append(span);
    expect(isDeliberateNavigation(dispatchOn(span))).toBe(true);
  });

  test("a new-tab link does not count: the page keeps running", () => {
    expect(
      isDeliberateNavigation(dispatchOn(anchor({ target: "_blank" }))),
    ).toBe(false);
  });

  test("a download link does not count: the page keeps running", () => {
    expect(isDeliberateNavigation(dispatchOn(anchor({ download: "" })))).toBe(
      false,
    );
  });

  test("a modified click does not count: it opens a tab or window", () => {
    for (const modifier of ["ctrlKey", "metaKey", "shiftKey", "altKey"]) {
      const event = dispatchOn(anchor(), { [modifier]: true });
      expect(isDeliberateNavigation(event)).toBe(false);
    }
  });

  test("a non-primary button does not count", () => {
    expect(isDeliberateNavigation(dispatchOn(anchor(), { button: 1 }))).toBe(
      false,
    );
    expect(isDeliberateNavigation(dispatchOn(anchor(), { button: 2 }))).toBe(
      false,
    );
  });

  test("a click that is not on a link does not count", () => {
    const button = document.createElement("button");
    button.textContent = "Next →";
    document.body.append(button);
    expect(isDeliberateNavigation(dispatchOn(button))).toBe(false);
  });
});
