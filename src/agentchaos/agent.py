"""Scripted agent for deterministic mode. Never calls an LLM.

It mimics the most common agent behaviour around failures: if a tool call
times out or returns a server error, call it again. It trusts API responses
and does not look at actual state.
"""

from dataclasses import dataclass, field
from typing import Any

from agentchaos.interfaces import CallNext, Context, ProxyRequest, WorkflowSpec
from agentchaos.proxy import Recorder


@dataclass
class AgentResult:
    believed_outcome: str  # success | failed
    context: Context
    completed_steps: list[str] = field(default_factory=list)
    failed_step: str | None = None


class ScriptedAgent:
    def __init__(
        self,
        spec: WorkflowSpec,
        send: CallNext,
        recorder: Recorder,
        run_id: str,
        max_attempts: int = 3,
    ) -> None:
        self._spec = spec
        self._send = send
        self._recorder = recorder
        self._run_id = run_id
        self._max_attempts = max_attempts

    async def run(self, workflow_input: dict[str, Any]) -> AgentResult:
        ctx: Context = dict(workflow_input)
        result = AgentResult("success", ctx)
        for step in self._spec.steps:
            done = False
            for attempt in range(1, self._max_attempts + 1):
                request = step.build(ctx)
                self._recorder.emit(
                    "agent",
                    "tool_call",
                    step=step.name,
                    attempt=attempt,
                    method=request.method,
                    path=request.path,
                    service=request.service,
                )
                response = await self._send(
                    ProxyRequest(self._run_id, step.name, attempt, request, context=dict(ctx))
                )
                self._recorder.emit(
                    "agent",
                    "observation",
                    step=step.name,
                    attempt=attempt,
                    status=response.status_code,
                    timed_out=response.timed_out,
                    source=response.source,
                )
                if response.ok:
                    step.capture(ctx, response.body)
                    result.completed_steps.append(step.name)
                    done = True
                    break
                if not response.ambiguous:
                    self._decide(step.name, "give_up", f"client error {response.status_code}")
                    break
                reason = "timeout" if response.timed_out else f"status {response.status_code}"
                if attempt < self._max_attempts:
                    self._decide(step.name, "retry", f"{reason}; assuming the call did not happen")
                else:
                    self._decide(step.name, "give_up", f"{reason} after {attempt} attempts")
            if done:
                continue
            if step.optional:
                self._decide(step.name, "skip_optional", "optional step failed; continuing")
                continue
            result.believed_outcome = "failed"
            result.failed_step = step.name
            break
        self._recorder.emit(
            "agent",
            "finished",
            believed_outcome=result.believed_outcome,
            failed_step=result.failed_step,
        )
        return result

    def _decide(self, step: str, action: str, reason: str) -> None:
        self._recorder.emit("agent", "decision", step=step, action=action, reason=reason)
