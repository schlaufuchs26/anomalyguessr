import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// The scene handle chip's legibility is a style property, not a DOM
// behaviour, so it is pinned at the source level (ticket #1433): the resting
// chip must not dim itself with `opacity`, must carry its own colour and
// background instead of inheriting the UA button colour (black, which made
// "AG-118" invisible on the dark page), and its text/background pair must
// clear WCAG AA (4.5:1) as small text. The contrast math mirrors the WCAG 2
// relative-luminance rule.

const css = readFileSync(join(import.meta.dir, "..", "gallery.css"), "utf8")
  // Comments first: they quote the old declarations and would confuse the
  // small declaration parser below.
  .replace(/\/\*[\s\S]*?\*\//g, "");

/** Declarations of the flat rule `selector` (assumes no nested blocks). */
function rule(selector: string): string {
  const start = css.indexOf(`\n${selector} {`);
  if (start === -1) throw new Error(`stylesheet has no rule: ${selector}`);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  if (open === -1 || close === -1) {
    throw new Error(`unbalanced rule: ${selector}`);
  }
  return css.slice(open + 1, close);
}

/** One declaration's value, or null when the property is absent. */
function decl(body: string, prop: string): string | null {
  const re = new RegExp(`(?:^|;)\\s*${prop}\\s*:\\s*([^;]+)`);
  const m = body.match(re);
  return m?.[1]?.trim() ?? null;
}

/** Theme values from the `:root` block of the same stylesheet. */
const THEME = Object.fromEntries(
  [...rule(":root").matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [
    m[1] as string,
    (m[2] as string).trim(),
  ]),
);

/** Resolve `var(--x)` against the theme; plain values pass through. */
function resolve(value: string | null, prop: string): string {
  if (!value) throw new Error(`missing declaration: ${prop}`);
  const m = value.match(/var\((--[\w-]+)\)/);
  if (!m) return value.trim();
  const resolved = THEME[m[1] as string];
  if (!resolved) throw new Error(`no :root value for ${m[1]}`);
  return resolved;
}

/** The colour inside a shorthand like `1px solid var(--text-muted)`. */
function colourOf(value: string | null, prop: string): string {
  const m = (value ?? "").match(/var\((--[\w-]+)\)|#[0-9a-fA-F]{6}/);
  if (!m) throw new Error(`${prop} carries no resolvable colour: ${value}`);
  return resolve(m[0], prop);
}

/** WCAG 2 relative luminance of a `#rrggbb` colour. */
function luminance(hex: string): number {
  const channel = (i: number) => {
    const c = Number.parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}

/** WCAG 2 contrast ratio between two `#rrggbb` colours. */
function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return ((hi as number) + 0.05) / ((lo as number) + 0.05);
}

describe("handle chip legibility (ticket #1433)", () => {
  const handle = rule(".td-handle");

  test("does not dim itself with opacity", () => {
    // The old rule used opacity: .75, which dimmed the text and its border.
    expect(decl(handle, "opacity")).toBeNull();
  });

  test("carries its own colour and a non-transparent background", () => {
    const background = resolve(decl(handle, "background"), "background");
    expect(background.toLowerCase()).not.toBe("transparent");
    expect(background.toLowerCase()).not.toBe("none");
    expect(decl(handle, "color")).not.toBeNull();
  });

  test("text clears WCAG AA against the chip's own background", () => {
    const text = colourOf(decl(handle, "color"), "color");
    const background = colourOf(decl(handle, "background"), "background");
    // Small text (0.85em): AA needs 4.5:1.
    expect(contrast(text, background)).toBeGreaterThanOrEqual(4.5);
    // And the published value, so a theme change that lowers it is visible
    // in the test name as well.
    expect(contrast(text, background)).toBeCloseTo(14.8, 0);
  });

  test("the border is visible against the chip and the page", () => {
    const background = colourOf(decl(handle, "background"), "background");
    const border = colourOf(decl(handle, "border"), "border");
    // WCAG 1.4.11 wants 3:1 for the boundary of a UI component: #8a8f98
    // measures 4.8:1 on the chip and 6.1:1 on the page.
    expect(contrast(border, background)).toBeGreaterThanOrEqual(3);
    expect(contrast(border, "#08090a")).toBeGreaterThanOrEqual(3);
  });

  test("keeps an id-sized chip with a usable click target", () => {
    // Digits must stay readable as an id: monospace, no shrinking games.
    expect(decl(handle, "font-family")).toContain("--font-mono");
    expect(decl(handle, "font-size")).toBe("0.85em");
    // 24px is the WCAG 2.2 minimum target size (AA).
    expect(decl(handle, "min-height")).toBe("24px");
    expect(decl(handle, "flex-shrink")).toBe("0");
  });
});
