"""Model decisions, tool execution and human waiting nodes for the shared graph."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from langgraph.types import interrupt
from pydantic import TypeAdapter, ValidationError

from tool_agent_lab.agent.model_client import ModelClient, ModelConfig
from tool_agent_lab.agent.config import AgentConfig, load_agent_config
from tool_agent_lab.agent.context import ContextError, bounded_tool_result, model_context
from tool_agent_lab.agent.inputs import InputService
from tool_agent_lab.agent.state import AgentState, identity_of
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.runtime.task_service import TaskError, change_status, load_current_attempt
from tool_agent_lab.schemas.common import NonEmptyStr
from tool_agent_lab.schemas.results import ConclusionDraft, QuestionDraft, build_result
from tool_agent_lab.schemas.tasks import TaskStatus
from tool_agent_lab.storage.database import connect, transaction
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS
from tool_agent_lab.tools.executor import ToolExecutor

@dataclass
class AgentNodes:
    database: Path
    data_dir: Path
    rules: BusinessRules
    model: ModelClient
    config: AgentConfig = field(default_factory=load_agent_config)

    def __post_init__(self) -> None:
        values = self.model.config.model_dump()
        values['api_key'] = self.model.config.api_key
        values['max_tokens'] = min(self.model.config.max_tokens, self.config.budgets.max_tokens)
        self.model.config = ModelConfig.model_validate(values)

    def executor(self, state: AgentState) -> ToolExecutor:
        return ToolExecutor(
            self.database, self.rules, identity_of(state), data_dir=self.data_dir,
            clarification_attempted=state["clarification_attempted"],
        )

    def status(self, state: AgentState, status: TaskStatus, event=None) -> None:
        identity = identity_of(state)
        with transaction(self.database) as connection:
            tasks = TaskRepository(connection)
            _, attempt = load_current_attempt(tasks, identity.task_id, identity.owner_id)
            if any(getattr(identity, k) != getattr(attempt, k) for k in type(identity).model_fields):
                raise TaskError("attempt_identity_mismatch")
            events = EventRepository(connection)
            change_status(tasks, events, attempt, status, self.rules.business_time)
            if event:
                events.append(identity, event[0], self.rules.business_time, payload=event[1])

    async def decide(self, state: AgentState) -> dict:
        if state["model_calls"] >= self.config.budgets.max_decisions:
            return {"result": {"outcome": "failed", "reason": "decision_budget_exceeded"}, "status": "failed"}
        tools = tuple(TOOL_CONTRACTS.values())
        context_state = state | {"facts": state["facts"] | {
            "business_time": self.rules.business_time.isoformat(),
            "business_rule_refs": {name: self.rules.reference(name).model_dump(mode="json")
                                   for name in ("request_refund", "issue_coupon", "create_handoff")},
        }}
        try:
            messages = model_context(context_state, self.config.prompt, tools=tools, max_bytes=self.config.budgets.max_context_bytes)
        except ContextError as error:
            return {"result": {"outcome": "failed", "reason": str(error)}, "status": "failed"}
        reply = await self.model.generate(messages, tools=tools)
        updates = {"model_calls": state["model_calls"] + 1}
        if reply.finish_reason in ("length", "timeout"):
            return updates | {"result": {"outcome": "failed", "reason": "model_" + reply.finish_reason}, "status": "failed"}
        messages = state["messages"] + [reply.message.as_message()]
        if len(reply.message.tool_calls) > self.config.budgets.max_tool_calls_per_turn:
            return updates | {"messages": messages, "result": {"outcome": "failed", "reason": "tool_call_budget_exceeded"}, "status": "failed"}
        if reply.message.tool_calls:
            return updates | {"messages": messages, "pending_calls": [c.model_dump(mode="json") for c in reply.message.tool_calls]}
        text = reply.message.content or ""
        try:
            control = json.loads(text)
        except json.JSONDecodeError:
            control = {"kind": "final", "summary": text}
        try:
            draft = TypeAdapter(QuestionDraft | ConclusionDraft).validate_python(control)
        except ValidationError:
            return updates | {"messages": messages, "result": {"outcome": "failed", "reason": "invalid_conclusion"}, "status": "failed"}
        if isinstance(draft, QuestionDraft):
            return updates | {"messages": messages, "waiting": {
                "kind": "input", "request_id": f"input-{uuid4().hex}", "question": draft.question,
            }}
        return updates | {"messages": messages, "result": {"outcome": "answered", "summary": draft.summary}}

    def select_tool(self, state: AgentState) -> dict:
        return {"active_call": state["pending_calls"][0], "pending_calls": state["pending_calls"][1:]}

    def tool_result(self, state: AgentState, result: dict) -> dict:
        call = state["active_call"]
        return {
            "messages": state["messages"] + [{"role": "tool", "tool_call_id": call["id"],
                                             "content": bounded_tool_result(call["function"]["name"], result, self.config.budgets.max_tool_output_bytes)}],
            "tool_results": state["tool_results"] + [{"tool": call["function"]["name"], "result": result}],
            "active_call": None, "proposal": None, "waiting": None,
        }

    def failure(self, state: AgentState, code: str) -> dict:
        return self.tool_result(state, {"call_id": state["active_call"]["id"], "status": "error",
            "error": {"code": code, "message": code, "outcome": "not_committed"}})

    def propose(self, state: AgentState) -> dict:
        call = state["active_call"]
        try:
            proposal = self.executor(state).propose(call["function"]["name"], json.loads(call["function"]["arguments"]))
        except (json.JSONDecodeError, ValidationError, TaskError) as error:
            return self.failure(state, error.code if isinstance(error, TaskError) else "invalid_arguments")
        return {"proposal": proposal.model_dump(mode="json"), "status": "waiting_approval"}

    def approval_receipt(self, state: AgentState, request_id: str):
        identity = identity_of(state)
        with connect(self.database) as connection:
            approval = TaskRepository(connection).get_approval(identity.task_id, request_id, identity.owner_id)
        if approval is None or approval.proposal.model_dump(mode="json") != state["proposal"]:
            raise TaskError("confirmation_required")
        return approval

    def wait_approval(self, state: AgentState) -> dict:
        request_id = interrupt({"kind": "approval", "proposal": state["proposal"]})
        approval = self.approval_receipt(state, request_id)
        receipt = {"approval_receipts": state["approval_receipts"] + [approval.model_dump(mode="json")]}
        if approval.request.decision == "rejected":
            return self.failure(state, "approval_rejected") | {"status": "running"} | receipt
        return {"status": "running"} | receipt

    async def execute(self, state: AgentState) -> dict:
        call = state["active_call"]
        try:
            arguments = json.loads(call["function"]["arguments"])
        except json.JSONDecodeError:
            return self.failure(state, "invalid_arguments")
        with transaction(self.database) as connection:
            EventRepository(connection).append(identity_of(state), "tool_call", self.rules.business_time,
                call_id=call["id"], payload={"tool": call["function"]["name"], "arguments": arguments})
        reply = await self.executor(state).call_tool(call["function"]["name"], arguments, call_id=call["id"])
        result = reply.result.model_dump(mode="json")
        logged_result = json.loads(bounded_tool_result(call["function"]["name"], result, self.config.budgets.max_tool_output_bytes))
        with transaction(self.database) as connection:
            EventRepository(connection).append(identity_of(state), "tool_result", self.rules.business_time,
                call_id=call["id"], payload={"tool": call["function"]["name"], "result": logged_result})
        update = self.tool_result(state, result)
        if result["status"] == "ok":
            name, data = call["function"]["name"], result["data"]
            facts = dict(state["facts"])
            if name == "get_order":
                facts["orders"] = dict(facts.get("orders", {})) | {data["order_id"]: data}
            elif name == "get_operation":
                facts["operation_lookups"] = dict(facts.get("operation_lookups", {})) | {data["operation_key"]: data}
            elif name in ("search_policy", "read_policy"):
                refs = [hit["reference"] for hit in data["hits"]] if name == "search_policy" else [data["reference"]]
                update["citations"] = list({json.dumps(ref, sort_keys=True): ref for ref in state["citations"] + refs}.values())
            update["facts"] = facts
        if result["status"] == "ok" and not TOOL_CONTRACTS[call["function"]["name"]].read_only:
            update["operations"] = state["operations"] + [result["data"]]
        return update

    def request_input(self, state: AgentState) -> dict:
        self.status(state, TaskStatus.WAITING_INPUT, ("input_requested", state["waiting"]))
        return {"status": "waiting_input"}

    def input_receipt(self, state: AgentState, request_id: str):
        identity = identity_of(state)
        receipt = InputService(self.database, business_time=self.rules.business_time).get(
            identity.task_id, request_id, owner_id=identity.owner_id,
        )
        if receipt is None or receipt.input_request_id != state["waiting"]["request_id"]:
            raise TaskError("input_receipt_required")
        return receipt

    def wait_input(self, state: AgentState) -> dict:
        request_id = TypeAdapter(NonEmptyStr).validate_python(interrupt(state["waiting"]))
        receipt = self.input_receipt(state, request_id)
        return {"messages": state["messages"] + [{"role": "user", "content": receipt.message}],
                "input_receipts": state["input_receipts"] + [receipt.model_dump(mode="json")],
                "clarification_attempted": True, "waiting": None, "status": "running"}

    def finish(self, state: AgentState) -> dict:
        result = build_result(state, self.database)
        status = TaskStatus.FAILED if state["status"] == "failed" else TaskStatus.COMPLETED
        self.status(state, status)
        return {"status": status.value, "result": result.model_dump(mode="json")}
