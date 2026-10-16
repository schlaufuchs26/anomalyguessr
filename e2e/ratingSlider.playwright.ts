import { expect, test } from "@playwright/test";
import { answerScene, openGame } from "./fixtures";

/**
 * The score slider's request behaviour (#2666), measured in a real browser:
 * moving the slider must not talk to the backend, the verdict call must carry
 * the score, and a score-only change on an already-decided scene must persist
 * on its own after the release. The test counts the POSTs the page issues per
 * phase, so a regression that re-introduces a request per slider step fails
 * here.
 *
 * The ten steps are arrow keys, not mouse clicks on the track (ticket #2669):
 * a raw `page.mouse.click` at a `boundingBox` coordinate assumes the slider
 * still sits where it sat when the box was measured, and on the CI runner the
 * moderation box scrolled out from under those fixed coordinates, so the
 * slider kept its start value and the test failed there while passing here.
 * Arrow keys fire the same one `input` event per step, wherever the slider
 * sits; the pointer-release path itself is covered by `tests/app.test.tsx`.
 */

test("moving the slider posts nothing until the verdict carries the score (#2666)", async ({
  page,
}) => {
  let ratingPosts = 0;
  let moderatePosts = 0;
  await page.route("**/anomalyguessr/api/scenes/*/rating", (route) => {
    if (route.request().method() === "POST") ratingPosts++;
    return route.fulfill({ json: { rating: 8 } });
  });
  await page.route("**/anomalyguessr/api/scenes/*/moderate", (route) => {
    if (route.request().method() === "POST") moderatePosts++;
    return route.fulfill({ json: { id: "smoke", moderation: "accepted" } });
  });

  await openGame(page);
  await answerScene(page);

  const slider = page.getByRole("slider", { name: "Quality rating" });
  await expect(slider).toHaveValue("0");

  // Ten steps up the scale, each an input the old code posted on its own.
  for (let i = 1; i <= 10; i++) {
    await slider.press("ArrowRight");
  }

  console.log(`MEASURE slider steps=10 ratingPosts=${ratingPosts}`);
  expect(ratingPosts).toBe(0);
  await expect(slider).toHaveValue("10");
  await expect(page.getByText(/not saved yet/)).toBeVisible();

  await page.getByRole("button", { name: "✓ Accept" }).click();
  await expect.poll(() => moderatePosts).toBe(1);
  expect(ratingPosts).toBe(0);
  // The verdict carried the score, so the box shows it as saved.
  await expect(page.getByText("Rating 10/10.")).toBeVisible();

  // A score-only change on the now-decided scene saves itself after the
  // release: one request, the debounce (250 ms) plus the round trip.
  const releasedAt = Date.now();
  await slider.press("ArrowLeft");
  await expect.poll(() => ratingPosts).toBe(1);
  console.log(
    `MEASURE release->saved ${Date.now() - releasedAt} ms (debounce 250 ms)`,
  );
  // The server confirmed it, so the "not saved yet" hint is gone.
  await expect(page.getByText(/not saved yet/)).toHaveCount(0);
});
