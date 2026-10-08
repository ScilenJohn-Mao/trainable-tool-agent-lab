"""Shared isolated setup and reply checks for MCP integration tests."""

import json
from pathlib import Path

import pytest

from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.runtime.task_service import TaskService
from tool_agent_lab.schemas.actions import ApprovalRequest
from tool_agent_lab.schemas.business import Order
from tool_agent_lab.schemas.tasks import AttemptIdentity, TaskCreate
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import initialize_database, transaction
from tool_agent_lab.tools.executor import ToolExecutor

DATA = PROJECT_ROOT / "data/business/v1"


@pytest.fixture
def case(tmp_path: Path):
    path = tmp_path / "app.sqlite3"
    initialize_database(path)
    with transaction(path) as connection:
        for row in json.loads((DATA / "orders.json").read_text(encoding="utf-8")):
            BusinessRepository(connection).add_order(Order.model_validate(row))
    rules = BusinessRules.from_file(DATA / "spec.json")
    tasks = TaskService(path, business_time=rules.business_time)
    task = tasks.create(TaskCreate(user_message="after sales", order_id="ORD-1001"), owner_id="demo-user")
    attempt = tasks.start(task.task_id, owner_id="demo-user")
    identity = AttemptIdentity(**{field: getattr(attempt, field) for field in AttemptIdentity.model_fields})
    executor = ToolExecutor(path, rules, identity, data_dir=DATA)
    return path, rules, tasks, identity, executor


def arguments(rules, name="request_refund"):
    values = {"order_id": "ORD-1001" if name == "request_refund" else "ORD-1002", "reason": "verified damage",
              "amount_minor": 12900 if name == "request_refund" else 500}
    if name == "create_handoff":
        values = {"order_id": "ORD-1004", "reason": "unsupported_category", "summary": "manual review"}
    return values | {"policy_refs": [rules.reference(name).model_dump(mode="json")]}


def approve(case, proposal, decision="approved", request_id="approval-1"):
    path, rules, tasks, identity, executor = case
    return ApprovalService(path, business_time=rules.business_time).record(identity.task_id, ApprovalRequest(
        request_id=request_id, proposal_id=proposal.proposal_id,
        proposal_version=proposal.proposal_version, decision=decision,
    ), owner_id=identity.owner_id)



def assert_reply(reply, call_id: str, *, error: str | None = None) -> None:
    assert reply.result.call_id == call_id
    assert reply.raw.structuredContent == reply.result.model_dump(mode="json")
    assert len(reply.raw.content) == 1 and reply.raw.content[0].type == "text"
    assert json.loads(reply.raw.content[0].text) == reply.raw.structuredContent
    assert reply.raw.isError == (error is not None)
    if error:
        assert reply.result.status == "error" and reply.result.error.code == error
        assert reply.result.error.outcome == "not_committed"
    else:
        assert reply.result.status == "ok"


