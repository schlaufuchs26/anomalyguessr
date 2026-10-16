import { expect, test } from "@playwright/test";
import { answerScene, openGame } from "./fixtures";

/**
 * The score slider's request behaviour (#2666), measured in a real browser:
 * moving the slider must not talk to the backend, the verdict call must carry
 * the score, and a score-only change on an already-decided scene must persist
 * on its own after the release. The test counts the POSTs the page issues per
 * phase, so a regression that re-introduces a request per slider step fails
 * here.
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
  const box = await slider.boundingBox();
  if (!box) throw new Error("the slider has no layout box");
  const y = box.y + box.height / 2;

  // Ten steps across the track, each an input the old code posted on its own.
  for (let i = 1; i <= 10; i++) {
    await page.mouse.click(box.x + ((box.width - 2) * i) / 10, y);
    await page.waitForTimeout(25);
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
  await page.mouse.move(box.x + (box.width - 2) * 0.4, y);
  await page.mouse.down();
  await page.mouse.up();
  await expect.poll(() => ratingPosts).toBe(1);
  console.log(
    `MEASURE release->saved ${Date.now() - releasedAt} ms (debounce 250 ms)`,
  );
  // The server confirmed it, so the "not saved yet" hint is gone.
  await expect(page.getByText(/not saved yet/)).toHaveCount(0);
});
