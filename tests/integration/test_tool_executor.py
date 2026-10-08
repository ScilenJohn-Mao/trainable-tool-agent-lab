"""Verify runtime bindings through real MCP writes and persisted authorization."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect, transaction
from tool_agent_lab.storage.task_repository import TaskRepository
from tool_agent_lab.tools.client import local_server_parameters, open_tool_session
from tool_agent_lab.tools.executor import ToolExecutor
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS

from mcp_support import DATA, approve, arguments, assert_reply, case


def call(executor, name, values, call_id="call-1"):
    reply = asyncio.run(executor.call_tool(name, values, call_id=call_id))
    assert_reply(reply, call_id, error=reply.result.error.code if reply.raw.isError else None)
    return reply.result


@pytest.mark.parametrize("name", ["request_refund", "issue_coupon", "create_handoff"])
def test_approved_real_mcp_writes_and_new_executor_reuse_original_key(case, name):
    path, rules, tasks, identity, executor = case
    values = arguments(rules, name)
    proposal = executor.propose(name, values)
    approve(case, proposal)
    result = call(executor, name, values)
    assert result.status == "ok", result
    assert result.data.status == "succeeded"
    another = ToolExecutor(path, rules, identity, data_dir=DATA)
    retry = call(another, name, values, "retry")
    assert retry.data == result.data
    lookup = call(another, "get_operation", {"operation_key": result.data.operation_key}, "query")
    assert lookup.data.operation.model_dump(mode="json") == result.data.model_dump(mode="json")
    with connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
        assert TaskRepository(connection).approval_consumed_at(identity.task_id, "approval-1", identity.owner_id)
        order = BusinessRepository(connection).get_order(values["order_id"], identity.owner_id)
        assert order.refunded_amount_minor == (12900 if name == "request_refund" else 0)
        assert order.coupon_amount_minor == (500 if name == "issue_coupon" else 0)


def test_prepare_commits_pending_before_transport_and_does_not_consume_approval(case, monkeypatch):
    import tool_agent_lab.tools.executor as module
    path, rules, tasks, identity, executor = case
    values = arguments(rules)
    proposal = executor.propose("request_refund", values)
    approve(case, proposal)
    actual_open = module.open_tool_session

    @asynccontextmanager
    async def inspect(parameters):
        with connect(path) as connection:
            repo = BusinessRepository(connection)
            pending = repo.get_operation_for_approval("approval-1", identity.owner_id)
            assert pending.status == "pending" and pending.amount_minor == 12900
            assert repo.get_order("ORD-1001", identity.owner_id).refunded_amount_minor == 0
            assert TaskRepository(connection).approval_consumed_at(identity.task_id, "approval-1", identity.owner_id) is None
            assert pending.operation_key in parameters.env["TTAL_MCP_CONTEXT"]
        async with actual_open(parameters) as client:
            yield client
    monkeypatch.setattr(module, "open_tool_session", inspect)
    assert call(executor, "request_refund", values).status == "ok"


@pytest.mark.parametrize("stage,code", [
    ("no_proposal", "proposal_required"), ("no_approval", "confirmation_required"),
    ("rejected", "confirmation_required"), ("changed", "proposal_mismatch"),
    ("wrong_amount", "amount_mismatch"),
])
def test_unconfirmed_rejected_or_changed_parameters_do_not_write(case, stage, code):
    path, rules, tasks, identity, executor = case
    values = arguments(rules)
    if stage == "wrong_amount":
        values["amount_minor"] = 1
    if stage != "no_proposal":
        proposal = executor.propose("request_refund", values)
        if stage != "no_approval":
            approve(case, proposal, "rejected" if stage == "rejected" else "approved")
    if stage == "changed":
        values["reason"] = "changed after approval"
    result = call(executor, "request_refund", values)
    assert result.error.code == code and result.error.outcome == "not_committed"
    with connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
        assert BusinessRepository(connection).get_order("ORD-1001", identity.owner_id).refunded_amount_minor == 0


def test_changed_proposal_requires_new_confirmation(case):
    path, rules, tasks, identity, executor = case
    values = arguments(rules)
    first = executor.propose("request_refund", values)
    approve(case, first)
    changed = values | {"reason": "new description"}
    second = executor.propose("request_refund", changed)
    assert second.proposal_version == first.proposal_version + 1
    assert call(executor, "request_refund", changed).error.code == "confirmation_required"
    approve(case, second, request_id="approval-2")
    assert call(executor, "request_refund", changed).status == "ok"


def test_same_order_new_approval_cannot_duplicate_refund(case):
    path, rules, tasks, identity, executor = case
    values = arguments(rules)
    approve(case, executor.propose("request_refund", values))
    assert call(executor, "request_refund", values).status == "ok"
    approve(case, executor.propose("request_refund", values), request_id="approval-2")
    assert call(executor, "request_refund", values).error.code == "business_action_already_committed"
    with connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1


def test_model_context_or_key_injection_is_refused(case):
    path, rules, tasks, identity, executor = case
    values = arguments(rules)
    for extra in [{"execution_context": executor.context.model_dump(mode="json")},
                  {"operation_key": "model-key"}, {"clarification_attempted": True}, {"owner_id": "demo-user"}]:
        assert call(executor, "request_refund", values | extra).error.code == "invalid_arguments"
    assert call(executor, "unregistered", {}).error.code == "unknown_tool"


def test_server_rechecks_cancel_after_prepare_and_metadata_cannot_replace_binding(case):
    path, rules, tasks, identity, executor = case
    values = arguments(rules)
    approve(case, executor.propose("request_refund", values))
    action = TOOL_CONTRACTS["request_refund"].parse_arguments(values)
    context = executor._prepare(action)
    with transaction(path) as connection:
        TaskRepository(connection).set_status(identity.task_id, identity.owner_id, "cancelled")

    async def run():
        params = local_server_parameters(database=path, data_dir=DATA, owner_id=identity.owner_id, execution_context=context)
        async with open_tool_session(params) as client:
            raw = await client.session.call_tool("request_refund", values, meta={
                "tool_agent_lab_call_id": "cancelled", "execution_context": context.model_dump(mode="json"),
            })
            assert raw.structuredContent["error"]["code"] == "task_not_executable"
    asyncio.run(run())
    with connect(path) as connection:
        assert BusinessRepository(connection).get_operation(context.write_binding.operation_key, identity.owner_id).status == "pending"
        assert TaskRepository(connection).approval_consumed_at(identity.task_id, "approval-1", identity.owner_id) is None
        assert BusinessRepository(connection).get_order("ORD-1001", identity.owner_id).refunded_amount_minor == 0


def test_runtime_operation_query_rejects_wrong_attempt_identity(case):
    path, rules, tasks, identity, executor = case
    bad = identity.model_copy(update={"thread_id": "wrong-thread"})
    other = ToolExecutor(path, rules, bad, data_dir=DATA)
    assert call(other, "get_operation", {"operation_key": "missing"}).error.code == "attempt_identity_mismatch"


def test_reserved_context_environment_is_not_inherited(case, monkeypatch):
    monkeypatch.setenv("TTAL_MCP_CONTEXT", "forged")
    monkeypatch.setenv("TTAL_MCP_CLARIFIED", "1")
    parameters = local_server_parameters()
    assert "TTAL_MCP_CONTEXT" not in parameters.env and "TTAL_MCP_CLARIFIED" not in parameters.env


def test_mcp_ledger_failure_rolls_back_money_and_approval(case):
    path, rules, tasks, identity, executor = case
    values = arguments(rules)
    approve(case, executor.propose("request_refund", values))
    with transaction(path) as connection:
        connection.execute("CREATE TRIGGER fail_ledger BEFORE UPDATE ON operations BEGIN SELECT RAISE(ABORT, 'ledger unavailable'); END")
    result = call(executor, "request_refund", values)
    assert result.error.code == "database_error" and result.error.outcome == "not_committed"
    with connect(path) as connection:
        assert BusinessRepository(connection).get_order("ORD-1001", identity.owner_id).refunded_amount_minor == 0
        operation = BusinessRepository(connection).get_operation_for_approval("approval-1", identity.owner_id)
        assert operation.status == "pending" and operation.result is None
        assert TaskRepository(connection).approval_consumed_at(identity.task_id, "approval-1", identity.owner_id) is None


def test_other_owner_cannot_propose_or_execute(case):
    from tool_agent_lab.runtime.task_service import TaskError
    path, rules, tasks, identity, executor = case
    values = arguments(rules)
    approve(case, executor.propose("request_refund", values))
    other = ToolExecutor(path, rules, identity.model_copy(update={"owner_id": "another-owner"}), data_dir=DATA)
    with pytest.raises(TaskError, match="task_not_found"):
        other.propose("request_refund", values)
    assert call(other, "request_refund", values).error.code == "proposal_required"
    with connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


def test_executor_reads_use_real_runtime_session(case):
    path, rules, tasks, identity, executor = case
    order = call(executor, "get_order", {"order_id": "ORD-1001"})
    assert order.data.paid_amount_minor == 12900 and order.data.refunded_amount_minor == 0
    found = call(executor, "search_policy", {"query": "500", "limit": 1})
    reference = found.data.hits[0].reference
    assert reference.version == "mock-policy-v1"
    read = call(executor, "read_policy", {"policy_id": reference.policy_id,
                                         "version": reference.version, "section": reference.section})
    assert read.data.text == found.data.hits[0].excerpt


@pytest.mark.parametrize("clarified,code", [(False, "clarification_required"), (True, None)])
def test_missing_order_handoff_only_uses_runtime_clarification_fact(case, clarified, code):
    path, rules, tasks, identity, executor = case
    runtime = ToolExecutor(path, rules, identity, data_dir=DATA, clarification_attempted=clarified)
    values = {"reason": "order_unresolved", "summary": "operator could not resolve order",
              "policy_refs": [rules.reference("create_handoff").model_dump(mode="json")]}
    approve(case, runtime.propose("create_handoff", values))
    result = call(runtime, "create_handoff", values)
    if code:
        assert result.error.code == code
    else:
        assert result.data.action == "create_handoff" and result.data.status == "succeeded"
