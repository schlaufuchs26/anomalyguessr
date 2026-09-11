import { existsSync } from "node:fs";
import { defineConfig } from "@playwright/test";

/**
 * Browser smoke tests for the layout regressions happy-dom cannot see (it has
 * no layout engine): the laptop two-pane layout must keep the HUD a bounded
 * scroller and the moderation box reachable (tickets #1185, #1187, #1193).
 *
 * The suite boots the real dev server (`bun index.html`), so the shipped CSS
 * and the React tree run together. CI installs Playwright's own Chromium
 * (`playwright install chromium`); this box ships Chromium via nix and the
 * Playwright CDN build lacks system libs here, so point the launcher at the
 * nix binary when it exists. PLAYWRIGHT_CHROMIUM_PATH overrides both.
 */
const localChromium =
  process.env.PLAYWRIGHT_CHROMIUM_PATH ??
  "/home/exedev/.nix-profile/bin/chromium";

const PORT = 4173;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.playwright.ts",
  timeout: 30_000,
  // CI annotates the PR with failures and keeps a browsable report; locally a
  // plain list is enough.
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  webServer: {
    command: "bun index.html",
    url: `http://localhost:${PORT}`,
    reuseExistingServer: !process.env.CI,
    env: { PORT: String(PORT) },
  },
  use: {
    baseURL: `http://localhost:${PORT}`,
    launchOptions: {
      ...(existsSync(localChromium) ? { executablePath: localChromium } : {}),
      args: ["--no-sandbox"],
    },
  },
});
