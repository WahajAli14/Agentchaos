"""The three v1 fault policies.

A policy targets one workflow step, chosen by the run's seeded RNG. Faults only
apply to the agent's own step requests, never to verification reads or
compensations.
"""

import random
from dataclasses import dataclass

from agentchaos.interfaces import FaultDecision, FaultPolicy, ProxyRequest

PASS = FaultDecision()


def _targets(request: ProxyRequest, step: str) -> bool:
    return request.purpose == "step" and request.step == step


@dataclass(frozen=True)
class TimeoutAfterCommit:
    """The first attempt reaches the service and commits; the response is lost."""

    target_step: str
    name: str = "timeout_after_commit"

    def decide(self, request: ProxyRequest) -> FaultDecision:
        if _targets(request, self.target_step) and request.attempt == 1:
            return FaultDecision(
                "timeout_after_commit",
                self.name,
                "The agent sees a timeout, but the request was delivered and processed.",
            )
        return PASS


@dataclass(frozen=True)
class DuplicateDelivery:
    """The first attempt is delivered to the service twice; the agent sees one response."""

    target_step: str
    name: str = "duplicate_delivery"

    def decide(self, request: ProxyRequest) -> FaultDecision:
        if _targets(request, self.target_step) and request.attempt == 1:
            return FaultDecision(
                "duplicate_delivery",
                self.name,
                "The agent sent one request and saw one response; the service received two.",
            )
        return PASS


@dataclass(frozen=True)
class PartialCommit:
    """Earlier steps succeed, then the target step's service is down for every attempt."""

    target_step: str
    name: str = "partial_commit"

    def decide(self, request: ProxyRequest) -> FaultDecision:
        if _targets(request, self.target_step):
            return FaultDecision(
                "fail_before_commit",
                self.name,
                "The service is unavailable; nothing was delivered. Earlier steps stay committed.",
            )
        return PASS


FAULT_TYPES = ("timeout_after_commit", "duplicate_delivery", "partial_commit")

# Steps each fault may hit. partial_commit needs at least one earlier committed
# step, and skips the optional notification (its failure does not fail the workflow).
ELIGIBLE_STEPS: dict[str, tuple[str, ...]] = {
    "timeout_after_commit": (
        "create_order",
        "reserve_inventory",
        "create_invoice",
        "confirm_order",
        "notify_customer",
    ),
    "duplicate_delivery": (
        "create_order",
        "reserve_inventory",
        "create_invoice",
        "confirm_order",
        "notify_customer",
    ),
    "partial_commit": ("reserve_inventory", "create_invoice", "confirm_order"),
}


def make_policy(fault_type: str, rng: random.Random, target_step: str | None = None) -> FaultPolicy:
    """Build a policy; the target step comes from the seeded RNG unless given."""
    step = target_step or rng.choice(ELIGIBLE_STEPS[fault_type])
    if fault_type == "timeout_after_commit":
        return TimeoutAfterCommit(step)
    if fault_type == "duplicate_delivery":
        return DuplicateDelivery(step)
    if fault_type == "partial_commit":
        return PartialCommit(step)
    raise ValueError(f"unknown fault type: {fault_type}")
