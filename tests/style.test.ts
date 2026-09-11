import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// Layout regressions are invisible to happy-dom (no layout engine), so the
// laptop height chain is guarded at the source level: the stylesheet must
// keep the HTML -> body -> #root -> main chain complete inside the laptop
// media query. See ticket #1187: the React port added <div id="root"> as the
// mount point, main{height:100%} stopped resolving, and the HUD grew past
// the viewport instead of scrolling (moderation box unreachable).
// Comments are stripped so the tiny rule matcher below only sees real syntax.
const css = readFileSync(
  join(import.meta.dir, "..", "style.css"),
  "utf8",
).replace(/\/\*[\s\S]*?\*\//g, "");

const LAPTOP_QUERY = "@media (min-width: 900px) and (min-height: 560px)";

/** Body of the first block whose header starts with `header`, via brace matching. */
function blockBody(source: string, header: string): string {
  const start = source.indexOf(header);
  if (start === -1) {
    throw new Error(`stylesheet has no block starting with: ${header}`);
  }
  const open = source.indexOf("{", start);
  if (open === -1) throw new Error(`block has no opening brace: ${header}`);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") {
      depth--;
      if (depth === 0) return source.slice(open + 1, i);
    }
  }
  throw new Error(`unbalanced braces in block: ${header}`);
}

/** Declarations of the flat rule for `selector` inside `block`, or null. */
function declarations(block: string, selector: string): string | null {
  const re = new RegExp(`(?:^|})\\s*${selector}\\s*\\{([^}]*)\\}`);
  const match = block.match(re);
  return match ? match[1] : null;
}

const laptop = blockBody(css, LAPTOP_QUERY);

describe("laptop layout height chain (ticket #1187)", () => {
  test("html and body lock to the viewport", () => {
    const rule = declarations(laptop, "html,\\s*body");
    expect(rule).not.toBeNull();
    expect(rule).toMatch(/height:\s*100%/);
  });

  test("#root passes the viewport height through to main", () => {
    const rule = declarations(laptop, "#root");
    expect(rule).not.toBeNull();
    expect(rule).toMatch(/height:\s*100%/);
  });

  test("main fills the mount point and becomes a flex column", () => {
    const rule = declarations(laptop, "main");
    expect(rule).not.toBeNull();
    expect(rule).toMatch(/height:\s*100%/);
    expect(rule).toMatch(/display:\s*flex/);
  });

  test("the HUD stays a bounded scroller", () => {
    const rule = declarations(laptop, "\\.hud");
    expect(rule).not.toBeNull();
    expect(rule).toMatch(/overflow-y:\s*auto/);
    expect(rule).toMatch(/min-height:\s*0/);
  });

  test("no height is forced on #root outside the laptop query", () => {
    // Narrow screens keep the auto-height mount point and scroll the page.
    const narrow = css.replace(blockBody(css, LAPTOP_QUERY), "");
    expect(narrow).not.toMatch(/#root\s*\{[^}]*height/);
  });
});

describe("the mode links keep the button look (ticket #1228)", () => {
  test("the mode link suppresses the default anchor underline", () => {
    const rule = declarations(css, "\\.mode-btn");
    expect(rule).not.toBeNull();
    expect(rule).toMatch(/text-decoration:\s*none/);
  });
});
