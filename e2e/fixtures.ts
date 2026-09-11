import { expect, type Page } from "@playwright/test";

/**
 * Shared harness for the Playwright suites (extracted when the #1225 unload
 * guard needed the same game as the layout suite). It stubs the manifest so
 * the tests are deterministic and need no queue backend: one scene with a
 * data-URL image, moderation mode on, and enough post-guess copy for the HUD
 * rail to overflow at laptop size.
 */

export const LAPTOP = { width: 1280, height: 757 };

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

/** Route `scenes/manifest.json` to the fixture. */
export async function stubManifest(page: Page): Promise<void> {
  await page.route("**/scenes/manifest.json", (route) =>
    route.fulfill({ json: MANIFEST }),
  );
}

/** Load the game at laptop size with the stubbed manifest. */
export async function openGame(page: Page): Promise<void> {
  await page.setViewportSize(LAPTOP);
  await stubManifest(page);
  await page.goto("/");
  // #1214: the app lands on the frontpage; the smoke flow plays the dev
  // moderation queue (the stubbed manifest carries `moderation: true`).
  await page.getByTestId("mode-moderation").click();
  await expect(page.locator("#scene-title")).toHaveText("Smoke test market");
  await expect(page.locator("#photo-img")).toBeVisible();
  // The reveal layer preloads per scene; wait for it before guessing.
  await page.waitForFunction(
    () => (document.querySelector("#photo-orig") as HTMLImageElement)?.complete,
  );
}

/** Click the photo center, which lands on the scene's center answer. */
export async function answerScene(page: Page): Promise<void> {
  const box = await page.locator("#overlay").boundingBox();
  if (!box) throw new Error("the photo overlay has no layout box");
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.locator("#result .headline")).toBeVisible();
}

/** Load the frontpage at laptop size with the stubbed manifest. */
export async function openFrontpage(page: Page): Promise<void> {
  await page.setViewportSize(LAPTOP);
  await stubManifest(page);
  await page.goto("/");
  await expect(page.getByTestId("mode-daily")).toBeVisible();
}
