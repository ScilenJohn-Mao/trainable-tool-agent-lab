"""Build and invoke the shared model-driven after-sales graph."""

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import TypeAdapter

from tool_agent_lab.agent.inputs import InputRequest, InputService
from tool_agent_lab.agent.nodes import AgentNodes
from tool_agent_lab.agent.state import AgentState, load_agent_state
from tool_agent_lab.runtime.task_service import TaskError, TaskService
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.schemas.actions import ApprovalRequest
from tool_agent_lab.schemas.common import NonEmptyStr
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS


def after_decision(state):
    if state["result"] is not None:
        return "finish"
    return "request_input" if state["waiting"] else "select_tool"


def after_tool(state):
    return "select_tool" if state["pending_calls"] else "decide"


def build_graph(nodes: AgentNodes, *, checkpointer):
    graph = StateGraph(AgentState)
    for name in ("decide", "select_tool", "propose", "wait_approval", "execute", "request_input", "wait_input", "finish"):
        graph.add_node(name, getattr(nodes, name))
    graph.add_edge(START, "decide")
    graph.add_conditional_edges("decide", after_decision)
    graph.add_conditional_edges("select_tool", lambda s: (
        "propose" if s["active_call"]["function"]["name"] in TOOL_CONTRACTS
        and TOOL_CONTRACTS[s["active_call"]["function"]["name"]].requires_approval else "execute"
    ))
    graph.add_conditional_edges("propose", lambda s: "wait_approval" if s["proposal"] else after_tool(s))
    graph.add_conditional_edges("wait_approval", lambda s: "execute" if s["active_call"] else after_tool(s))
    graph.add_conditional_edges("execute", after_tool)
    graph.add_edge("request_input", "wait_input")
    graph.add_edge("wait_input", "decide")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)


class Agent:
    def __init__(self, nodes: AgentNodes, *, checkpointer) -> None:
        self.nodes = nodes
        self.graph = build_graph(nodes, checkpointer=checkpointer)

    def binding(self, task_id: str, owner_id: str):
        state = load_agent_state(self.nodes.database, task_id, owner_id=owner_id, model_version=self.nodes.model.config.version)
        if state["identity"]["config_version"] != self.nodes.config.version:
            raise TaskError("config_version_mismatch")
        budgets = self.nodes.config.budgets
        return state, {"configurable": {"thread_id": state["identity"]["thread_id"]},
                       "recursion_limit": budgets.max_decisions * (4 * budgets.max_tool_calls_per_turn + 3) + 8}

    async def start(self, task_id: str, *, owner_id: str):
        state, config = self.binding(task_id, owner_id)
        if (await self.graph.aget_state(config)).values:
            raise TaskError("graph_already_started")
        TaskService(self.nodes.database, business_time=self.nodes.rules.business_time).start(task_id, owner_id=owner_id)
        state["status"] = "running"
        return await self.graph.ainvoke(state, config)

    async def snapshot(self, task_id: str, *, owner_id: str):
        state, config = self.binding(task_id, owner_id)
        snapshot = await self.graph.aget_state(config)
        if not snapshot.values or snapshot.values["identity"] != state["identity"]:
            raise TaskError("checkpoint_identity_mismatch")
        return snapshot

    async def resume(self, task_id: str, value, *, owner_id: str):
        value = TypeAdapter(NonEmptyStr).validate_python(value)
        snapshot = await self.snapshot(task_id, owner_id=owner_id)
        if snapshot.next == ("wait_input",):
            self.nodes.input_receipt(snapshot.values, value)
        elif snapshot.next == ("wait_approval",):
            self.nodes.approval_receipt(snapshot.values, value)
        else:
            raise TaskError("graph_not_waiting")
        _, config = self.binding(task_id, owner_id)
        return await self.graph.ainvoke(Command(resume=value), config)

    async def submit_input(self, task_id: str, request: InputRequest, *, owner_id: str):
        snapshot = await self.snapshot(task_id, owner_id=owner_id)
        receipt = InputService(self.nodes.database, business_time=self.nodes.rules.business_time).record(
            task_id, request, owner_id=owner_id,
        )
        if receipt.model_dump(mode="json") in snapshot.values["input_receipts"]:
            return snapshot.values
        return await self.resume(task_id, receipt.request_id, owner_id=owner_id)

    async def submit_approval(self, task_id: str, request: ApprovalRequest, *, owner_id: str):
        snapshot = await self.snapshot(task_id, owner_id=owner_id)
        receipt = ApprovalService(self.nodes.database, business_time=self.nodes.rules.business_time).record(
            task_id, request, owner_id=owner_id,
        )
        if receipt.model_dump(mode="json") in snapshot.values["approval_receipts"]:
            return snapshot.values
        return await self.resume(task_id, receipt.request.request_id, owner_id=owner_id)
