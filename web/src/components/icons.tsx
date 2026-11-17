import type { ReactNode } from "react";

function Svg({ children, size = 14 }: { children: ReactNode; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="shrink-0">
      {children}
    </svg>
  );
}

export const CheckIcon = () => <Svg><circle cx="8" cy="8" r="6.25" /><path d="m5.25 8.25 2 2 3.5-4" /></Svg>;
export const HalfIcon = () => <Svg><circle cx="8" cy="8" r="6.25" /><path d="M8 1.75v12.5" /><path d="M8 4.5h3.9M8 8h4.9M8 11.5h3.9" /></Svg>;
export const HandIcon = () => <Svg><path d="M3 14V2.5" /><path d="M3 3h8.5l-1.75 3 1.75 3H3" /></Svg>;
export const CrossIcon = () => <Svg><path d="M5.4 1.75h5.2l3.65 3.65v5.2l-3.65 3.65H5.4L1.75 10.6V5.4z" /><path d="m6 6 4 4m0-4-4 4" /></Svg>;
export const UnknownIcon = () => <Svg><circle cx="8" cy="8" r="6.25" /><path d="M6.4 6.3a1.7 1.7 0 1 1 2.4 1.6c-.5.3-.8.7-.8 1.2" /><path d="M8 11.4v.1" /></Svg>;
export const WarnIcon = () => <Svg><path d="M8 2 14.5 13.5h-13z" /><path d="M8 6.5v3.25M8 11.6v.1" /></Svg>;
export const SunIcon = () => <Svg><circle cx="8" cy="8" r="3" /><path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M3.4 12.6l1-1M11.6 4.4l1-1" /></Svg>;
export const MoonIcon = () => <Svg><path d="M13.5 9.5A5.75 5.75 0 0 1 6.5 2.5a5.75 5.75 0 1 0 7 7z" /></Svg>;
export const SortIcon = ({ direction }: { direction: "asc" | "desc" }) => (
  <Svg size={10}><path d={direction === "asc" ? "m4 10 4-4 4 4" : "m4 6 4 4 4-4"} /></Svg>
);
