import { describe, expect, it } from "vitest";
import type { TimelineEvent } from "./api";
import { buildRows, describe as describeEvent } from "./timeline";

const ev = (seq: number, lane: TimelineEvent["lane"], kind: string, ms: number, payload: Record<string, unknown>): TimelineEvent =>
  ({ seq, lane, kind, logical_ms: ms, payload });

const events = [
  ev(0, "agent", "tool_call", 40, { step: "create_invoice", attempt: 1, method: "POST", service: "billing", path: "/invoices" }),
  ev(1, "proxy", "fault_injected", 40, { fault_type: "timeout_after_commit", step: "create_invoice", attempt: 1 }),
  ev(2, "proxy", "delivered", 60, { step: "create_invoice", purpose: "step", method: "POST", service: "billing", path: "/invoices", status: 201 }),
  ev(3, "ground_truth", "state_change", 60, { service: "billing", table: "invoices", id: "inv_1", change: "inserted", row: { order_id: "ord_1", status: "issued" } }),
  ev(4, "proxy", "response_to_agent", 1060, { step: "create_invoice", attempt: 1, status: null, timed_out: true }),
  ev(5, "agent", "observation", 1060, { step: "create_invoice", attempt: 1, status: null, timed_out: true, source: "service" }),
];
const mismatches = [{ seq: 1, logical_ms: 40, step: "create_invoice", fault_type: "timeout_after_commit", explanation: "Lost response." }];

describe("buildRows", () => {
  const rows = buildRows({ events, mismatches });

  it("shows each logical time once on the shared axis and reports gaps", () => {
    expect(rows.map((r) => r.showTime)).toEqual([true, false, true, false, true, false]);
    expect(rows[4].gapMs).toBe(1000);
  });

  it("pairs what the agent was told with what the database shows", () => {
    expect(rows[1].mismatch).toEqual({
      explanation: "Lost response.",
      told: "TIMEOUT",
      actual: ["billing.invoices inv_1 inserted"],
    });
    expect(rows.filter((r) => r.mismatch)).toHaveLength(1);
  });
});

describe("describe", () => {
  it("names the real thing for each event kind", () => {
    expect(describeEvent(events[5]).title).toBe("Sees TIMEOUT for create_invoice");
    expect(describeEvent(events[3]).detail).toContain("inv_1 order_id=ord_1 status=issued");
    expect(describeEvent(ev(9, "agent", "something_new", 0, { a: 1 })).title).toBe("something_new");
  });
});
