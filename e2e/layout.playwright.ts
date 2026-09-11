import { expect, type Page, test } from "@playwright/test";

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
 * The manifest is stubbed so the suite is deterministic and needs no queue
 * backend: one scene with a data-URL image, moderation mode on, and enough
 * post-guess copy to overflow the HUD rail.
 */

const LAPTOP = { width: 1280, height: 757 };

/** 4:3 placeholder with real dimensions, so the stage can fit the photo. */
const IMAGE = `data:image/svg+xml,${encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600"><rect width="800" height="600" fill="#3b4a5a"/></svg>',
)}`;

/** Long, scene-typical copy: the HUD must scroll to hold it at 757px tall. */
const EXPLANATION =
  "Sealed plastic snack bags with printed branding only became common in the " +
  "mid-20th century, so one cannot lie on a market stall in 1900; the sealed " +
  "foil wrapper was patented in the 1950s and reached European corner shops " +
  "only after the war.";

const MANIFEST = {
  version: 2,
  date: "2026-09-10",
  moderation: true,
  scenes: [
    {
      id: "smoke",
      title: "Smoke test market",
      place: "Dresden",
      year: "1900",
      credit: "Public Domain",
      sourceUrl: "https://example.org/smoke",
      source: {
        repository: "Test repository",
        fileUrl: "https://example.org/file",
        originalTitle: "Test original",
        date: "1900",
        place: "Dresden",
        license: "Public Domain",
        description: "A test scene.",
      },
      image: IMAGE,
      original: IMAGE,
      anomaly: "Chip bag (modern snack bag)",
      description:
        "A crowded open-air market: baskets and sacks of vegetables and " +
        "eggs, umbrellas over the stalls, vendors and buyers packed between " +
        "the goods.",
      explanation: EXPLANATION,
      references: [
        { label: "Potato chip", url: "https://example.org/potato-chip" },
        { label: "Plastic", url: "https://example.org/plastic" },
      ],
      answer: { x: 0.5, y: 0.5, r: 0.05 },
      hints: ["It lies on the ground.", "Left half.", "A modern chip bag."],
    },
  ],
};

/** Load the game at laptop size with the stubbed manifest. */
async function openGame(page: Page): Promise<void> {
  await page.setViewportSize(LAPTOP);
  await page.route("**/scenes/manifest.json", (route) =>
    route.fulfill({ json: MANIFEST }),
  );
  await page.goto("/");
  await expect(page.locator("#scene-title")).toHaveText("Smoke test market");
  await expect(page.locator("#photo-img")).toBeVisible();
  // The reveal layer preloads per scene; wait for it before guessing.
  await page.waitForFunction(
    () => (document.querySelector("#photo-orig") as HTMLImageElement)?.complete,
  );
}

/** Click the photo center, which lands on the scene's center answer. */
async function answerScene(page: Page): Promise<void> {
  const box = await page.locator("#overlay").boundingBox();
  if (!box) throw new Error("the photo overlay has no layout box");
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.locator("#result .headline")).toBeVisible();
}

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
