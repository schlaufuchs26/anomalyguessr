import { expect, test } from "@playwright/test";
import {
  answerScene,
  LAPTOP,
  openFrontpage,
  openGame,
  stubManifest,
} from "./fixtures";

/**
 * Laptop-viewport layout smoke test (ticket #1193).
 *
 * The React port regressed twice without any test noticing: #1185 dropped the
 * `img.complete` check so a correct guess never revealed the original, and
 * #1187 lost the HTML -> body -> #root -> main height chain so the HUD grew
 * past the viewport and the dev moderation box became unreachable. happy-dom
 * cannot see either (no layout engine), so this suite drives real Chromium at
 * a short laptop viewport (1366x768 minus browser chrome) and asserts the
 * layout contract end to end.
 *
 * The game fixture the flows run on lives in ./fixtures.ts, shared with the
 * unload-guard suite (#1225).
 */

test("the frontpage offers the mode links and fits the laptop (#1214)", async ({
  page,
}) => {
  await openFrontpage(page);
  const daily = page.getByTestId("mode-daily");
  const moderation = page.getByTestId("mode-moderation");
  await expect(daily).toBeVisible();
  await expect(moderation).toBeVisible();
  // keyboard play starts on the first mode link
  await expect(daily).toBeFocused();

  // the frontpage must not push the page past the viewport (height chain)
  const doc = await page.evaluate(() => ({
    scrollHeight: document.scrollingElement?.scrollHeight ?? 0,
    innerHeight: window.innerHeight,
  }));
  expect(doc.scrollHeight).toBeLessThanOrEqual(doc.innerHeight + 1);
});

test("the HUD is the bounded scroller on the laptop layout", async ({
  page,
}) => {
  await openGame(page);
  await answerScene(page);

  const hud = await page.locator(".hud").evaluate((el) => {
    const style = getComputedStyle(el);
    return {
      overflowY: style.overflowY,
      minHeight: style.minHeight,
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
    };
  });
  expect(hud.overflowY).toBe("auto");
  expect(hud.minHeight).toBe("0px");
  // The post-guess panel overflows the rail, so the HUD must scroll it.
  expect(hud.scrollHeight).toBeGreaterThan(hud.clientHeight);

  // The page itself must not scroll: the whole layout fits the viewport.
  const doc = await page.evaluate(() => ({
    scrollHeight: document.scrollingElement?.scrollHeight ?? 0,
    innerHeight: window.innerHeight,
  }));
  expect(doc.scrollHeight).toBeLessThanOrEqual(doc.innerHeight + 1);
});

test("a correct guess reveals the original photo (#1185)", async ({ page }) => {
  await openGame(page);
  await answerScene(page);

  // The click hit the center answer, so the original dissolves in. Before the
  // fix the layer never got its `reveal` class (no load event was coming).
  await expect(page.locator("#photo-orig")).toHaveClass(/reveal/);
});

test("the gallery menu sits top-right inside the laptop viewport (#1204)", async ({
  page,
}) => {
  await openGame(page);

  const menu = page.locator('[data-testid="gallery-menu"]');
  await expect(menu).toBeVisible();
  await expect(menu).toHaveAttribute("href", "gallery/");
  await expect(menu).toHaveAttribute("title", "Overview of all images");

  const box = await menu.boundingBox();
  if (!box) throw new Error("the gallery menu has no layout box");
  // top-right and inside the viewport: the header row must not push the
  // layout (or the menu itself) off screen
  expect(box.x).toBeGreaterThan(LAPTOP.width / 2);
  expect(box.x + box.width).toBeLessThanOrEqual(LAPTOP.width + 1);
  expect(box.y + box.height).toBeLessThanOrEqual(LAPTOP.height + 1);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
});

test("the moderation box is reachable inside the laptop viewport (#1187)", async ({
  page,
}) => {
  await openGame(page);
  await answerScene(page);

  const moderate = page.locator("#moderate");
  await expect(moderate).toBeVisible();

  // #1187 broke the height chain: the main column grew past the viewport, the
  // body clipped it, and focusing the Next button scrolled the PAGE (scrollY
  // > 0) while the HUD rail stayed un-scrollable. The scroll that brings the
  // moderation box into view must be the HUD's own, and the page must stay put.
  const hud = page.locator(".hud");
  await hud.evaluate((el) => {
    el.scrollTop = el.scrollHeight;
  });
  expect(await hud.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);

  const box = await moderate.boundingBox();
  if (!box) throw new Error("the moderation box has no layout box");
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height).toBeLessThanOrEqual(LAPTOP.height + 1);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
});

test("a mode path deep-links, reloads and Back returns (#1223)", async ({
  page,
}) => {
  await page.setViewportSize(LAPTOP);
  await stubManifest(page);

  // The base URL is the frontpage.
  await page.goto("/");
  await expect(page.getByTestId("mode-daily")).toBeVisible();

  // Daily has its own path: a hard load starts the run, no frontpage.
  await page.goto("/daily");
  await expect(page.locator("#scene-title")).toHaveText("Smoke test market");
  await expect(page.getByTestId("mode-daily")).toHaveCount(0);

  // A reload keeps the mode.
  await page.reload();
  await expect(page.locator("#scene-title")).toHaveText("Smoke test market");

  // Back lands on the frontpage, Forward returns to the mode.
  await page.goBack();
  await expect(page.getByTestId("mode-daily")).toBeVisible();
  await page.goForward();
  await expect(page.locator("#scene-title")).toHaveText("Smoke test market");
});

test("a mode is a real link, so it opens in a new tab (#1228)", async ({
  page,
  context,
}) => {
  await page.setViewportSize(LAPTOP);
  // The popup is a fresh page: stub the manifest on the context, not the tab.
  await stubManifest(context);
  await page.goto("/");

  const daily = page.getByTestId("mode-daily");
  await expect(daily).toHaveAttribute("href", "/daily");

  // A middle click is the browser's own new-tab gesture; the app adds no
  // handler, so the tab follows the href.
  const popup = context.waitForEvent("page");
  await daily.click({ button: "middle" });
  const tab = await popup;
  await tab.waitForLoadState();
  await expect(tab.locator("#scene-title")).toHaveText("Smoke test market");
  await tab.close();

  // The original frontpage keeps running.
  await expect(page.getByTestId("mode-daily")).toBeVisible();
});
