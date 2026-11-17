import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// WCAG 2.1 contrast, computed from the token file itself.
function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}
function theme(css: string, selector: string): Record<string, string> {
  const block = css.slice(css.indexOf(selector));
  const body = block.slice(block.indexOf("{") + 1, block.indexOf("}"));
  return Object.fromEntries([...body.matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})/g)].map((m) => [m[1], m[2]]));
}

const css = readFileSync("src/tokens.css", "utf8");
const TEXT_TOKENS = ["text", "muted", "accent", "verified", "partial", "manual", "violation", "indeterminate"];

describe.each([["dark", '[data-theme="dark"]'], ["light", '[data-theme="light"]']])("%s theme", (_, selector) => {
  const t = theme(css, selector);
  it.each(TEXT_TOKENS)("%s meets WCAG AA (4.5:1) on bg, surface and raised", (name) => {
    for (const background of [t.bg, t.surface, t.raised]) {
      expect(contrast(t[name], background)).toBeGreaterThanOrEqual(4.5);
    }
  });
  it("accent button text meets AA on the accent colour", () => {
    expect(contrast(t["accent-text"], t.accent)).toBeGreaterThanOrEqual(4.5);
  });
  it("text stays readable on the mismatch highlight", () => {
    expect(contrast(t.text, t["mismatch-bg"])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(t.manual, t["mismatch-bg"])).toBeGreaterThanOrEqual(4.5);
  });
});
