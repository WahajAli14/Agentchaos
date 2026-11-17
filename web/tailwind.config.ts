import type { Config } from "tailwindcss";

// The theme is replaced, not extended: only token colours, the 12/13/14/16/20
// type scale and radii up to 6px exist. Default Tailwind palettes are unavailable.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    colors: {
      transparent: "transparent",
      current: "currentColor",
      bg: "var(--bg)",
      surface: "var(--surface)",
      raised: "var(--raised)",
      border: "var(--border)",
      "border-strong": "var(--border-strong)",
      text: "var(--text)",
      muted: "var(--muted)",
      accent: "var(--accent)",
      "accent-text": "var(--accent-text)",
      verified: "var(--verified)",
      partial: "var(--partial)",
      manual: "var(--manual)",
      violation: "var(--violation)",
      indeterminate: "var(--indeterminate)",
      mismatch: "var(--mismatch-bg)",
    },
    fontFamily: {
      sans: ['"IBM Plex Sans"', "system-ui", "sans-serif"],
      mono: ['"IBM Plex Mono"', "ui-monospace", "monospace"],
    },
    fontSize: {
      xs: ["12px", "16px"],
      sm: ["13px", "20px"],
      base: ["14px", "20px"],
      lg: ["16px", "24px"],
      xl: ["20px", "28px"],
    },
    borderRadius: { none: "0", sm: "2px", DEFAULT: "4px", md: "6px", dot: "9999px" },
    boxShadow: { none: "none" },
    transitionDuration: { DEFAULT: "150ms", fast: "120ms", slow: "200ms" },
    transitionTimingFunction: { DEFAULT: "cubic-bezier(0, 0, 0.2, 1)" },
    extend: {
      spacing: { row: "32px", nav: "192px", list: "560px" },
      gridTemplateColumns: { timeline: "72px repeat(3, minmax(0, 1fr))" },
    },
  },
  plugins: [],
} satisfies Config;
