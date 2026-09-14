import { expect, test } from "@playwright/test";
import { MANIFEST, openGame } from "./fixtures";

/**
 * The HUD score counter (#1490). Evan's request: the click hint shows only the
 * arrow, and the price of a miss shows as a running score on the right that
 * visibly drops. This drives the real browser so the drop is seen end to end:
 * hit scene A (100), then miss once on scene B (90), then resolve B (190).
 */

const FIRST = MANIFEST.scenes[0];
const SECOND = {
  ...FIRST,
  id: "smoke-2",
  title: "Smoke test square",
  answer: { x: 0.2, y: 0.8, r: 0.04 },
};
const TWO_SCENES = { ...MANIFEST, scenes: [FIRST, SECOND] };

test("the HUD counter drops by 10 on a miss (#1490)", async ({ page }) => {
  await openGame(page, TWO_SCENES);

  const counter = page.locator("#score-counter .hud-score-value");
  await expect(counter).toHaveText("0");

  // scene A: a center click is a first-try hit for 100
  const overlay = page.locator("#overlay");
  let box = await overlay.boundingBox();
  if (!box) throw new Error("the photo overlay has no layout box");
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.locator("#result .headline")).toBeVisible();
  await expect(counter).toHaveText("100");

  await page.getByRole("button", { name: "Next →" }).click();
  await expect(page.locator("#scene-title")).toHaveText("Smoke test square");

  // scene B: a far click is a miss, so the counter dips to 90 and flashes
  box = await overlay.boundingBox();
  if (!box) throw new Error("the photo overlay has no layout box");
  await page.mouse.click(box.x + box.width * 0.9, box.y + box.height * 0.1);
  await expect(page.locator(".marker.cue")).toBeVisible();
  await expect(page.locator("#score-counter")).toHaveClass(/dip/);
  await expect(counter).toHaveText("90");

  // the resolving hit on B scores 100 − 10, replacing the preview
  await page.mouse.click(box.x + box.width * 0.2, box.y + box.height * 0.8);
  await expect(page.locator("#result .headline")).toBeVisible();
  await expect(counter).toHaveText("190");
});
