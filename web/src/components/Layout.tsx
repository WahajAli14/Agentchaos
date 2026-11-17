import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { MoonIcon, SunIcon } from "./icons";

const NAV = [
  { to: "/scenarios", label: "Scenarios" },
  { to: "/runs", label: "Runs" },
  { to: "/benchmark", label: "Benchmark" },
];

function readTheme(): "dark" | "light" {
  try {
    return localStorage.getItem("theme") === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function Layout() {
  const [theme, setTheme] = useState(readTheme);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("theme", theme);
    } catch {
      /* theme still applies for this session */
    }
  }, [theme]);

  return (
    <div className="flex h-full">
      <nav className="flex w-nav shrink-0 flex-col border-r border-border bg-surface" aria-label="Main">
        <div className="border-b border-border px-4 py-3">
          <div className="text-base font-semibold">AgentChaos</div>
          <div className="text-xs text-muted">integration reliability lab</div>
        </div>
        <ul className="flex-1 py-2">
          {NAV.map((item) => (
            <li key={item.to}>
              <NavLink
                to={item.to}
                className={({ isActive }) =>
                  `block border-l-2 px-4 py-2 transition-colors ${
                    isActive ? "border-accent bg-raised font-medium text-text" : "border-transparent text-muted hover:text-text"
                  }`
                }
              >
                {item.label}
              </NavLink>
            </li>
          ))}
        </ul>
        <button
          type="button"
          onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          className="flex items-center gap-2 border-t border-border px-4 py-3 text-xs text-muted hover:text-text"
        >
          {theme === "dark" ? <SunIcon /> : <MoonIcon />}
          {theme === "dark" ? "Light theme" : "Dark theme"}
        </button>
      </nav>
      <main className="min-w-0 flex-1 overflow-hidden">
        <Outlet />
      </main>
    </div>
  );
}

export function PageHeader({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <header className="flex h-12 shrink-0 items-center justify-between gap-4 border-b border-border px-4">
      <h1 className="text-lg font-semibold">{title}</h1>
      <div className="flex items-center gap-2">{children}</div>
    </header>
  );
}
