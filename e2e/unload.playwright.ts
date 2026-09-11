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

  // Back to the game (a fresh document: the run itself is gone) and into the
  // frontpage through the in-app route, which never unloads the page.
  await page.goBack();
  await expect(page.locator("#scene-title")).toHaveText("Smoke test market");
  await page.evaluate(() => {
    window.location.hash = "";
  });
  await expect(page.getByTestId("mode-daily")).toBeVisible();
  expect(dialogs).toEqual([]);
});
