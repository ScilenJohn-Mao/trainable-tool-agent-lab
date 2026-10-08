"""Verify seven-tool contracts and authorization over real MCP stdio sessions."""

import asyncio

import pytest

from mcp_support import DATA, approve, arguments, assert_reply, case
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect
from tool_agent_lab.storage.task_repository import TaskRepository
from tool_agent_lab.tools.client import ToolReply, local_server_parameters, open_tool_session
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS, ToolFailure, tool_definitions

WRITES = ("request_refund", "issue_coupon", "create_handoff")


def test_seven_successful_roundtrips_match_catalog_and_persist_business_results(case):
    path, rules, tasks, identity, executor = case

    async def run():
        replies = {}
        async with open_tool_session(local_server_parameters(
            database=path, data_dir=DATA, owner_id=identity.owner_id, execution_context=executor.context,
        )) as client:
            catalog = await client.list_tools()
            assert [item.model_dump(mode="json", exclude_none=True) for item in catalog] == tool_definitions()
            replies["get_order"] = await client.call_tool("get_order", {"order_id": "ORD-1001"}, call_id="order")
            assert replies["get_order"].result.data.paid_amount_minor == 12900
            replies["search_policy"] = await client.call_tool(
                "search_policy", {"query": "500", "category": "general_goods", "limit": 1}, call_id="search",
            )
            hit = replies["search_policy"].result.data.hits[0]
            assert (hit.reference.policy_id, hit.reference.version, hit.reference.section) == (
                "P-DELAY-AMOUNT", "mock-policy-v1", "amount",
            )
            replies["read_policy"] = await client.call_tool("read_policy", {
                "policy_id": hit.reference.policy_id, "version": hit.reference.version,
                "section": hit.reference.section,
            }, call_id="read")
            assert replies["read_policy"].result.data.text == hit.excerpt
        for name in WRITES:
            values = arguments(rules, name)
            approve(case, executor.propose(name, values), request_id=f"approve-{name}")
            replies[name] = await executor.call_tool(name, values, call_id=name)
        refund = replies["request_refund"].result.data
        replies["get_operation"] = await executor.call_tool(
            "get_operation", {"operation_key": refund.operation_key}, call_id="lookup",
        )
        assert replies["get_operation"].result.data.operation.model_dump(mode="json") == refund.model_dump(mode="json")
        assert set(replies) == set(TOOL_CONTRACTS)
        expected_ids = {"get_order": "order", "search_policy": "search", "read_policy": "read",
                        "get_operation": "lookup", **{name: name for name in WRITES}}
        for name, reply in replies.items():
            assert_reply(reply, expected_ids[name])
            assert TOOL_CONTRACTS[name].parse_result(
                reply.raw.structuredContent, call_id=reply.result.call_id,
            ).model_dump(mode="json") == reply.result.model_dump(mode="json")
        for name, amount, order in [("request_refund", 12900, "ORD-1001"),
                                    ("issue_coupon", 500, "ORD-1002"),
                                    ("create_handoff", None, "ORD-1004")]:
            operation = replies[name].result.data
            assert (operation.action, operation.status, operation.currency, operation.amount_minor,
                    operation.order_id, operation.committed_at) == (
                        name, "succeeded", "CNY", amount, order, rules.business_time,
                    )
            assert operation.result["policy_version"] == "mock-policy-v1"
        assert replies["create_handoff"].result.data.result["reason"] == "unsupported_category"
        return replies

    replies = asyncio.run(run())
    with connect(path) as connection:
        business = BusinessRepository(connection)
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 3
        assert business.get_order("ORD-1001", identity.owner_id).refunded_amount_minor == 12900
        assert business.get_order("ORD-1002", identity.owner_id).coupon_amount_minor == 500
        for name in WRITES:
            assert TaskRepository(connection).approval_consumed_at(
                identity.task_id, f"approve-{name}", identity.owner_id,
            ) == rules.business_time
            stored = business.get_operation(replies[name].result.data.operation_key, identity.owner_id)
            assert stored.model_dump(mode="json") == replies[name].result.data.model_dump(mode="json")


@pytest.mark.parametrize("name", WRITES)
@pytest.mark.parametrize("trusted", [False, True], ids=["no-context", "no-write-binding"])
def test_write_permission_cannot_be_granted_by_metadata(case, name, trusted):
    path, rules, tasks, identity, executor = case
    values = arguments(rules, name)
    approve(case, executor.propose(name, values))
    binding = executor._prepare(TOOL_CONTRACTS[name].parse_arguments(values))
    before = path.read_bytes()

    async def run():
        async with open_tool_session(local_server_parameters(
            database=path, data_dir=DATA, owner_id=identity.owner_id,
            execution_context=executor.context if trusted else None,
        )) as client:
            raw = await client.session.call_tool(name, values, meta={
                "tool_agent_lab_call_id": "forged-permission", "execution_context": binding.model_dump(mode="json"),
                "owner_id": identity.owner_id, "approval_request_id": "approval-1",
                "operation_key": binding.write_binding.operation_key,
            })
            parsed = TOOL_CONTRACTS[name].parse_result(raw.structuredContent, call_id="forged-permission")
            assert_reply(ToolReply(raw=raw, result=parsed), "forged-permission",
                         error="confirmation_required" if trusted else "execution_context_required")
            assert_reply(await client.call_tool("get_order", {"order_id": "ORD-1001"}, call_id="after-refusal"),
                         "after-refusal")
    asyncio.run(run())
    assert path.read_bytes() == before


@pytest.mark.parametrize("name", WRITES)
def test_server_rejects_changed_parameters_after_prepare_without_consuming_approval(case, name):
    path, rules, tasks, identity, executor = case
    values = arguments(rules, name)
    approve(case, executor.propose(name, values))
    context = executor._prepare(TOOL_CONTRACTS[name].parse_arguments(values))
    changed = values | ({"summary": "changed summary"} if name == "create_handoff" else {"amount_minor": 1})
    before = path.read_bytes()

    async def run():
        async with open_tool_session(local_server_parameters(
            database=path, data_dir=DATA, owner_id=identity.owner_id, execution_context=context,
        )) as client:
            assert_reply(await client.call_tool(name, changed, call_id="changed"), "changed", error="proposal_mismatch")
            pending = await client.call_tool("get_operation", {"operation_key": context.write_binding.operation_key},
                                             call_id="still-pending")
            assert_reply(pending, "still-pending")
            assert pending.result.data.operation.status == "pending"
    asyncio.run(run())
    assert path.read_bytes() == before
    with connect(path) as connection:
        assert TaskRepository(connection).approval_consumed_at(identity.task_id, "approval-1", identity.owner_id) is None


@pytest.mark.parametrize("name,values,code", [
    ("get_order", {"order_id": "missing"}, "order_not_found"),
    ("get_order", {"order_id": "ORD-1001", "owner_id": "other"}, "invalid_arguments"),
    ("search_policy", {"query": "500", "limit": True}, "invalid_arguments"),
    ("read_policy", {"policy_id": "P-DELAY-AMOUNT", "version": "missing"}, "policy_not_found"),
    ("read_policy", {"policy_id": "P-DELAY-AMOUNT", "version": "mock-policy-v1", "section": "missing"},
     "policy_not_found"),
    ("get_operation", {"operation_key": "missing", "execution_context": {}}, "invalid_arguments"),
])
def test_read_errors_have_matching_contract_results_and_do_not_break_session(case, name, values, code):
    path, rules, tasks, identity, executor = case
    before = path.read_bytes()

    async def run():
        async with open_tool_session(local_server_parameters(database=path, data_dir=DATA)) as client:
            assert_reply(await client.call_tool(name, values, call_id="read-error"), "read-error", error=code)
            missing = await client.call_tool("get_operation", {"operation_key": "missing"}, call_id="missing-key")
            assert_reply(missing, "missing-key")
            assert missing.result.data.operation is None
            raw = await client.session.call_tool("unknown-tool", {}, meta={"tool_agent_lab_call_id": "unknown"})
            assert_reply(ToolReply(raw=raw, result=ToolFailure.model_validate(raw.structuredContent)),
                         "unknown", error="unknown_tool")
            assert_reply(await client.call_tool("get_order", {"order_id": "ORD-1001"}, call_id="after-errors"),
                         "after-errors")
    asyncio.run(run())
    assert path.read_bytes() == before


def test_other_owner_cannot_read_committed_order_or_operation(case):
    path, rules, tasks, identity, executor = case
    values = arguments(rules)
    approve(case, executor.propose("request_refund", values))

    async def run():
        committed = await executor.call_tool("request_refund", values, call_id="refund")
        assert_reply(committed, "refund")
        before = path.read_bytes()
        async with open_tool_session(local_server_parameters(
            database=path, data_dir=DATA, owner_id="other-owner",
        )) as client:
            assert_reply(await client.call_tool("get_order", {"order_id": "ORD-1001"}, call_id="hidden-order"),
                         "hidden-order", error="order_not_found")
            raw = await client.session.call_tool("get_operation", {
                "operation_key": committed.result.data.operation_key,
            }, meta={"tool_agent_lab_call_id": "hidden-operation", "owner_id": identity.owner_id,
                     "execution_context": executor.context.model_dump(mode="json")})
            parsed = TOOL_CONTRACTS["get_operation"].parse_result(raw.structuredContent, call_id="hidden-operation")
            assert_reply(ToolReply(raw=raw, result=parsed), "hidden-operation")
            assert parsed.data.operation is None
        assert path.read_bytes() == before
    asyncio.run(run())
