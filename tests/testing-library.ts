import { afterEach, expect } from "bun:test";
import * as matchers from "@testing-library/jest-dom/matchers";
import { cleanup } from "@testing-library/react";

// Extend expect with Testing Library matchers (toBeInTheDocument, etc.)
expect.extend(matchers);

// Auto-cleanup rendered components between tests
afterEach(() => {
  cleanup();
});

// TypeScript declarations for the Testing Library matchers
// biome-ignore lint/correctness/noUnusedImports: used in declaration merging below
import type { AsymmetricMatchers, Matchers } from "bun:test";
import type { TestingLibraryMatchers } from "@testing-library/jest-dom/matchers";

declare module "bun:test" {
  interface Matchers<T>
    extends TestingLibraryMatchers<typeof expect.stringContaining, T> {}
  interface AsymmetricMatchers extends TestingLibraryMatchers {}
}
