import { useEffect, useRef } from "react";

/** Single-key shortcuts. Ignored while typing in a form field or with a modifier held. */
export function useKeys(bindings: Record<string, (event: KeyboardEvent) => void>) {
  const latest = useRef(bindings);
  latest.current = bindings;
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      const typing = target && (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable);
      if (typing && event.key !== "Escape") return;
      const handler = latest.current[event.key];
      if (handler) {
        event.preventDefault();
        handler(event);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

export const SHORTCUTS: { keys: string; action: string }[] = [
  { keys: "j / k", action: "Move down / up in the list" },
  { keys: "Enter", action: "Open the highlighted row" },
  { keys: "r", action: "Run the selected scenario (Scenarios screen)" },
  { keys: "/", action: "Focus the filter (Runs screen)" },
  { keys: "?", action: "Show or hide this list" },
  { keys: "Esc", action: "Close this list, or leave a form field" },
];
