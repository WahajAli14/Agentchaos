"""Live mode: a real LLM agent with tool calling, and replay of what it decided.

The tools are the business APIs. Every call goes through the same safety
controls and fault proxy as the scripted agent, and every decision is recorded.
A recorded run can be replayed without an LLM (`ReplayAgent`), which is what
makes before/after comparisons of real agent behaviour deterministic.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentchaos.agent import AgentResult
from agentchaos.interfaces import (
    CallNext,
    Context,
    ProxyRequest,
    ProxyResponse,
    Purpose,
    StepRequest,
    WorkflowSpec,
)
from agentchaos.llm import LLMError, LLMProvider
from agentchaos.proxy import Recorder
from agentchaos.tracing import get_tracer

SYSTEM_PROMPT = """You are the order-processing assistant for a wholesale supplier.
You fulfil customer orders by calling the company's internal APIs through the tools provided.

To fulfil an order, in this sequence:
1. create_order
2. reserve_inventory for that order
3. create_invoice for that order (amount_cents = quantity x unit_price_cents)
4. confirm_order with the reservation id and invoice id
5. send_notification to tell the customer the order is confirmed

Use the ids returned by earlier calls. If a call fails or times out, decide what to do.
Call one tool at a time. When you are finished, reply with a short summary and end with
exactly one line: RESULT: SUCCESS if the order is confirmed, otherwise RESULT: FAILED."""

Builder = Callable[[dict[str, Any]], StepRequest]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict[str, dict[str, str]]
    step: str
    purpose: Purpose
    build: Builder

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": list(self.properties),
                },
            },
        }


STR, INT = {"type": "string"}, {"type": "integer"}

TOOLS: tuple[Tool, ...] = (
    Tool(
        "create_order",
        "Create a new customer order.",
        {"customer_id": STR, "sku": STR, "quantity": INT, "unit_price_cents": INT},
        "create_order",
        "step",
        lambda a: StepRequest("orders", "POST", "/orders", json=a),
    ),
    Tool(
        "reserve_inventory",
        "Reserve stock for an order.",
        {"order_id": STR, "sku": STR, "quantity": INT},
        "reserve_inventory",
        "step",
        lambda a: StepRequest("inventory", "POST", "/reservations", json=a),
    ),
    Tool(
        "create_invoice",
        "Issue an invoice for an order.",
        {"order_id": STR, "amount_cents": INT},
        "create_invoice",
        "step",
        lambda a: StepRequest("billing", "POST", "/invoices", json=a),
    ),
    Tool(
        "confirm_order",
        "Confirm an order once it has a reservation and an invoice.",
        {"order_id": STR, "reservation_id": STR, "invoice_id": STR},
        "confirm_order",
        "step",
        lambda a: StepRequest(
            "orders",
            "POST",
            f"/orders/{a['order_id']}/confirm",
            json={"reservation_id": a["reservation_id"], "invoice_id": a["invoice_id"]},
        ),
    ),
    Tool(
        "send_notification",
        "Notify the customer that their order is confirmed.",
        {"order_id": STR, "message": STR},
        "notify_customer",
        "step",
        lambda a: StepRequest(
            "notifications",
            "POST",
            "/notifications",
            json={"order_id": a["order_id"], "kind": "order_confirmed", "message": a["message"]},
        ),
    ),
    Tool(
        "get_order",
        "Look up an order.",
        {"order_id": STR},
        "confirm_order",
        "verify",
        lambda a: StepRequest("orders", "GET", f"/orders/{a['order_id']}"),
    ),
    Tool(
        "list_reservations",
        "List stock reservations for an order.",
        {"order_id": STR},
        "reserve_inventory",
        "verify",
        lambda a: StepRequest("inventory", "GET", "/reservations", params=a),
    ),
    Tool(
        "list_invoices",
        "List invoices for an order.",
        {"order_id": STR},
        "create_invoice",
        "verify",
        lambda a: StepRequest("billing", "GET", "/invoices", params=a),
    ),
    Tool(
        "release_reservation",
        "Release a stock reservation.",
        {"reservation_id": STR},
        "reserve_inventory",
        "compensation",
        lambda a: StepRequest("inventory", "POST", f"/reservations/{a['reservation_id']}/release"),
    ),
    Tool(
        "void_invoice",
        "Void an invoice.",
        {"invoice_id": STR},
        "create_invoice",
        "compensation",
        lambda a: StepRequest("billing", "POST", f"/invoices/{a['invoice_id']}/void"),
    ),
    Tool(
        "cancel_order",
        "Cancel an order that has not been confirmed.",
        {"order_id": STR},
        "create_order",
        "compensation",
        lambda a: StepRequest("orders", "POST", f"/orders/{a['order_id']}/cancel"),
    ),
)
TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}
ID_ARGS = ("order_id", "reservation_id", "invoice_id")


class ToolRunner:
    """Executes tool calls through controls and proxy; tracks what the agent knows."""

    def __init__(
        self, spec: WorkflowSpec, send: CallNext, recorder: Recorder, run_id: str, ctx: Context
    ) -> None:
        self._steps = {step.name: step for step in spec.steps}
        self._required = [step.name for step in spec.steps if not step.optional]
        self._send = send
        self._recorder = recorder
        self._run_id = run_id
        self.ctx = ctx
        self.completed: list[str] = []
        self.calls = 0
        self._attempts: dict[str, int] = {}

    def first_incomplete_step(self) -> str | None:
        return next((name for name in self._required if name not in self.completed), None)

    async def call(self, name: str, arguments: dict[str, Any]) -> ProxyResponse | str:
        """Returns the response, or an error string if the call could not be made."""
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            return f"unknown tool: {name}"
        missing = [key for key in tool.properties if key not in arguments]
        if missing:
            return f"missing arguments: {', '.join(missing)}"
        request = tool.build(arguments)
        attempt = self._attempts.get(name, 0) + 1
        self._attempts[name] = attempt
        self.calls += 1
        self._recorder.emit(
            "agent",
            "tool_call",
            step=tool.step,
            tool=name,
            attempt=attempt,
            method=request.method,
            path=request.path,
            service=request.service,
            arguments=arguments,
        )
        response = await self._send(
            ProxyRequest(
                self._run_id, tool.step, attempt, request, tool.purpose, context=dict(self.ctx)
            )
        )
        self._recorder.emit(
            "agent",
            "observation",
            step=tool.step,
            tool=name,
            attempt=attempt,
            status=response.status_code,
            timed_out=response.timed_out,
            source=response.source,
        )
        if tool.purpose == "step" and response.ok and isinstance(response.body, dict):
            self._steps[tool.step].capture(self.ctx, response.body)
            if tool.step not in self.completed:
                self.completed.append(tool.step)
        return response


def tool_result_text(result: ProxyResponse | str) -> str:
    if isinstance(result, str):
        return json.dumps({"error": result})
    if result.timed_out:
        return json.dumps({"error": "timeout: no response received from the service"})
    return json.dumps({"status": result.status_code, "body": result.body})


def believed_outcome(text: str | None) -> str:
    match = re.search(r"RESULT:\s*(SUCCESS|FAILED)", text or "", re.IGNORECASE)
    if match is None:
        return "unknown"
    return "success" if match.group(1).upper() == "SUCCESS" else "failed"


def _finish(
    runner: ToolRunner, recorder: Recorder, believed: str, trace: list[dict[str, Any]]
) -> AgentResult:
    failed = None if believed == "success" else runner.first_incomplete_step()
    recorder.emit("agent", "finished", believed_outcome=believed, failed_step=failed)
    return AgentResult(believed, runner.ctx, list(runner.completed), failed, trace=trace)


class LiveAgent:
    def __init__(
        self,
        provider: LLMProvider,
        spec: WorkflowSpec,
        send: CallNext,
        recorder: Recorder,
        run_id: str,
        max_turns: int = 20,
    ) -> None:
        self._provider = provider
        self._spec = spec
        self._send = send
        self._recorder = recorder
        self._run_id = run_id
        self._max_turns = max_turns

    async def run(self, workflow_input: dict[str, Any]) -> AgentResult:
        runner = ToolRunner(
            self._spec, self._send, self._recorder, self._run_id, dict(workflow_input)
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Please process this order: " + json.dumps(workflow_input),
            },
        ]
        schemas = [tool.schema() for tool in TOOLS]
        trace: list[dict[str, Any]] = []
        llm_calls = prompt_tokens = completion_tokens = 0
        believed, final_text = "unknown", None
        started = time.perf_counter()
        for turn in range(1, self._max_turns + 1):
            with get_tracer().start_as_current_span("agentchaos.llm.chat") as span:
                span.set_attribute("gen_ai.system", self._provider.name)
                span.set_attribute("gen_ai.request.model", self._provider.model)
                span.set_attribute("agentchaos.turn", turn)
                try:
                    reply = await self._provider.chat(messages, schemas)
                except LLMError as error:
                    span.set_attribute("agentchaos.llm_error", True)
                    self._recorder.emit("agent", "llm_error", turn=turn, error=str(error))
                    break
                span.set_attribute("gen_ai.usage.input_tokens", reply.prompt_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", reply.completion_tokens)
            llm_calls += 1
            prompt_tokens += reply.prompt_tokens
            completion_tokens += reply.completion_tokens
            self._recorder.emit(
                "agent",
                "llm_call",
                turn=turn,
                provider=self._provider.name,
                model=self._provider.model,
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
                tool_calls=[call.name for call in reply.tool_calls],
            )
            messages.append(reply.message)
            if not reply.tool_calls:
                final_text = reply.content
                believed = believed_outcome(final_text)
                self._recorder.emit("agent", "message", text=final_text or "")
                break
            for call in reply.tool_calls:
                result = await runner.call(call.name, call.arguments)
                trace.append(
                    {
                        "tool": call.name,
                        "arguments": call.arguments,
                        "ok": not isinstance(result, str) and result.ok,
                        "ambiguous": not isinstance(result, str) and result.ambiguous,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": tool_result_text(result),
                    }
                )
        trace.append({"believed_outcome": believed, "final_message": final_text})
        outcome = _finish(runner, self._recorder, believed, trace)
        outcome.llm_calls = llm_calls
        outcome.prompt_tokens = prompt_tokens
        outcome.completion_tokens = completion_tokens
        outcome.wall_latency_ms = (time.perf_counter() - started) * 1000
        return outcome


class ReplayAgent:
    """Replays a recorded live run's tool calls. Never calls an LLM.

    Two adjustments keep a replay meaningful when controls differ from the
    recording: id arguments are taken from the current run's context when known,
    and a recorded retry is skipped when the call it retried (ambiguous in the
    recording) succeeded this time. Each skip is recorded as an event.
    """

    def __init__(
        self,
        trace: list[dict[str, Any]],
        spec: WorkflowSpec,
        send: CallNext,
        recorder: Recorder,
        run_id: str,
    ) -> None:
        self._trace = trace
        self._spec = spec
        self._send = send
        self._recorder = recorder
        self._run_id = run_id

    async def run(self, workflow_input: dict[str, Any]) -> AgentResult:
        runner = ToolRunner(
            self._spec, self._send, self._recorder, self._run_id, dict(workflow_input)
        )
        believed = "unknown"
        resolved: tuple[str, str] | None = None
        for entry in self._trace:
            if "tool" not in entry:
                believed = entry.get("believed_outcome", "unknown")
                continue
            arguments = dict(entry["arguments"])
            for key in ID_ARGS:
                if key in arguments and key in runner.ctx:
                    arguments[key] = runner.ctx[key]
            signature = (entry["tool"], json.dumps(arguments, sort_keys=True))
            if resolved == signature:
                self._recorder.emit(
                    "agent",
                    "replay_skipped",
                    tool=entry["tool"],
                    reason="recorded retry of a call that was ambiguous in the recording "
                    "and succeeded in this run",
                )
                continue
            resolved = None
            result = await runner.call(entry["tool"], arguments)
            if entry.get("ambiguous") and not isinstance(result, str) and result.ok:
                resolved = signature
        return _finish(runner, self._recorder, believed, list(self._trace))
