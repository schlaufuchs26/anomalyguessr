import { expect, type Page, test } from "@playwright/test";
import { answerScene, openFrontpage, openGame } from "./fixtures";

/**
 * Reload guard smoke test (ticket #1225).
 *
 * happy-dom can only see that the app cancels a synthetic `beforeunload`
 * event (the unit suite); whether Chromium then really interrupts the
 * navigation is a browser behavior. This suite drives real Chromium, accepts
 * whatever dialog it raises and reads its type, so a regression that stops
 * calling `preventDefault` shows up here.
 */

/** Collect the dialogs Chromium raises, accepting each so navigation proceeds. */
function watchDialogs(page: Page): string[] {
  const types: string[] = [];
  page.on("dialog", (dialog) => {
    types.push(dialog.type());
    void dialog.accept();
  });
  return types;
}

test("a reload mid-run asks the browser first (#1225)", async ({ page }) => {
  await openGame(page);
  await answerScene(page);

  const dialogs = watchDialogs(page);
  await page.reload();
  expect(dialogs).toEqual(["beforeunload"]);
});

test("a reload before the first answer and on the frontpage is silent (#1225)", async ({
  page,
}) => {
  await openFrontpage(page);
  const dialogs = watchDialogs(page);
  await page.reload();
  expect(dialogs).toEqual([]);

  // A run with no answer yet has nothing to lose either.
  await page.getByTestId("mode-moderation").click();
  await expect(page.locator("#scene-title")).toHaveText("Smoke test market");
  await page.reload();
  expect(dialogs).toEqual([]);
});

test("the in-app moves (back to the frontpage, the gallery link) stay silent (#1225)", async ({
  page,
}) => {
  await openGame(page);
  await answerScene(page);

  // The gallery is a real page of its own, so stub it: the point here is the
  // deliberate-link handling, not the gallery itself.
  await page.route("**/gallery/", (route) =>
    route.fulfill({ contentType: "text/html", body: "<h1>Gallery</h1>" }),
  );
  const dialogs = watchDialogs(page);
  await Promise.all([
    page.waitForURL("**/gallery/"),
    page.getByTestId("gallery-menu").click(),
  ]);
  expect(dialogs).toEqual([]);

  // Back to the game (restored from the cache; the run is still there).
  await page.goBack();
  await expect(page.locator("#scene-title")).toHaveText("Smoke test market");

  // Back again, from the game to the frontpage entry the app pushed: an
  // in-app popstate route that never unloads the page.
  await page.goBack();
  await expect(page.getByTestId("mode-daily")).toBeVisible();
  expect(dialogs).toEqual([]);
});

test("a click on an in-app link mid-run stays silent (#1230)", async ({
  page,
}) => {
  await openGame(page);
  await answerScene(page);

  // The modes have real paths since #1223, so the mode and frontpage links
  // (#1228/#1220) replace the document the way the gallery link does. Render
  // the same anchor the frontpage will carry and stub its target, so the
  // navigation is deterministic.
  await page.route("**/daily", (route) =>
    route.fulfill({ contentType: "text/html", body: "<h1>Daily</h1>" }),
  );
  await page.evaluate(() => {
    const link = document.createElement("a");
    link.id = "mode-link";
    link.href = "daily";
    link.textContent = "Daily";
    document.body.append(link);
  });

  const dialogs = watchDialogs(page);
  await Promise.all([page.waitForURL("**/daily"), page.click("#mode-link")]);
  expect(dialogs).toEqual([]);
});

test("a link that opens a tab mid-run does not disarm the guard (#1230)", async ({
  page,
}) => {
  await openGame(page);
  await answerScene(page);

  // The references in the "Why?" box open in a new tab: this document keeps
  // running, so the next reload must still warn.
  await page.route("https://example.org/**", (route) =>
    route.fulfill({ contentType: "text/html", body: "<h1>Reference</h1>" }),
  );
  const popup = page.waitForEvent("popup");
  await page.getByRole("link", { name: "Potato chip" }).click();
  await (await popup).close();

  const dialogs = watchDialogs(page);
  await page.reload();
  expect(dialogs).toEqual(["beforeunload"]);
});
