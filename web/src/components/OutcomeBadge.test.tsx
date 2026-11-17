import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Outcome } from "../lib/api";
import { OutcomeBadge } from "./OutcomeBadge";

const ALL: Outcome[] = ["verified_recovery", "partial_recovery", "manual_intervention_required", "undetected_violation", "indeterminate"];

describe("OutcomeBadge", () => {
  it.each(ALL)("%s shows an icon and its label, not colour alone", (outcome) => {
    const { container } = render(<OutcomeBadge outcome={outcome} />);
    expect(screen.getByText(outcome)).toBeInTheDocument();
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("uses a different icon for every outcome", () => {
    const shapes = ALL.map((outcome) => render(<OutcomeBadge outcome={outcome} />).container.querySelector("svg")!.innerHTML);
    expect(new Set(shapes).size).toBe(ALL.length);
  });
});
