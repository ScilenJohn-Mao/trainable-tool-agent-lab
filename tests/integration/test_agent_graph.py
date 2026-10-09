import asyncio
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from scripts.seed_demo import seed_demo
from tool_agent_lab.agent.graph import Agent
from tool_agent_lab.agent.model_client import AssistantMessage, FunctionCall, ModelClient, ModelToolCall, load_model_config
from tool_agent_lab.agent.nodes import AgentNodes
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.runtime.task_service import TaskService
from tool_agent_lab.schemas.actions import ApprovalRequest
from tool_agent_lab.schemas.tasks import TaskCreate
from tool_agent_lab.settings import load_settings
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect


def tool_message(name, arguments, call_id):
    return AssistantMessage(tool_calls=(ModelToolCall(id=call_id, function=FunctionCall(
        name=name, arguments=json.dumps(arguments),
    )),))


def setup_agent(tmp_path, replies):
    settings = load_settings()
    database = tmp_path / "app.sqlite3"
    seed_demo(database, settings=settings)
    rules = BusinessRules.from_file(settings.business_data_dir / "spec.json")
    model = ModelClient(load_model_config(), mock_responses=replies)
    tasks = TaskService(database, business_time=rules.business_time, model_version=model.config.version)
    task = tasks.create(TaskCreate(user_message="Refund the damaged ORD-1001", order_id="ORD-1001"), owner_id=settings.dev_owner_id)
    nodes = AgentNodes(database, settings.business_data_dir, rules, model)
    return Agent(nodes, checkpointer=InMemorySaver()), tasks, task, rules


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_model_tools_pause_for_real_decision_then_use_protected_executor(tmp_path, decision):
    rules = BusinessRules.from_file(load_settings().business_data_dir / "spec.json")
    action = {"order_id": "ORD-1001", "amount_minor": 12900, "reason": "Verified damage",
              "policy_refs": [rules.reference("request_refund").model_dump(mode="json")]}
    agent, tasks, task, rules = setup_agent(tmp_path, [
        tool_message("get_order", {"order_id": "ORD-1001"}, "order-call"),
        tool_message("request_refund", action, "refund-call"),
        AssistantMessage(content='{"kind":"final","summary":"Decision processed."}'),
    ])

    async def run():
        paused = await agent.start(task.task_id, owner_id=task.owner_id)
        assert paused["__interrupt__"][0].value["kind"] == "approval"
        assert tasks.get(task.task_id, owner_id=task.owner_id).status == "waiting_approval"
        with connect(agent.nodes.database) as connection:
            assert BusinessRepository(connection).get_order("ORD-1001", task.owner_id).refunded_amount_minor == 0
        proposal = tasks.current_proposal(task.task_id, owner_id=task.owner_id)
        approval = ApprovalService(agent.nodes.database, business_time=rules.business_time).record(task.task_id, ApprovalRequest(
            request_id="human-decision", proposal_id=proposal.proposal_id,
            proposal_version=proposal.proposal_version, decision=decision,
        ), owner_id=task.owner_id)
        finished = await agent.resume(task.task_id, approval.request.request_id, owner_id=task.owner_id)
        assert finished["status"] == "completed" and finished["model_calls"] == 3
        assert finished["identity"]["thread_id"] == paused["identity"]["thread_id"]
        assert [m["tool_call_id"] for m in finished["messages"] if m["role"] == "tool"] == ["order-call", "refund-call"]
        with connect(agent.nodes.database) as connection:
            order = BusinessRepository(connection).get_order("ORD-1001", task.owner_id)
            receipt = BusinessRepository(connection).get_successful_operation("ORD-1001", "request_refund", task.owner_id)
        assert order.refunded_amount_minor == (12900 if decision == "approved" else 0)
        assert (receipt is not None) == (decision == "approved")
        assert len(finished["operations"]) == (1 if decision == "approved" else 0)

    asyncio.run(run())
