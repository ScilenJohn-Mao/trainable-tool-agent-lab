"""Exercise real MCP subprocesses, tool results and local identity isolation."""

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import types

from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.business.service import BusinessService
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.runtime.task_service import TaskService
from tool_agent_lab.schemas.actions import ApprovalRequest, ExecutionContext, RefundAction, WriteBinding
from tool_agent_lab.schemas.business import Order
from tool_agent_lab.schemas.tasks import AttemptIdentity, TaskCreate
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect, initialize_database, transaction
from tool_agent_lab.tools.client import ToolClient, local_server_parameters, open_tool_session
from tool_agent_lab.tools.contracts import ToolFailure, tool_definitions

from mcp_support import assert_reply

DATA = PROJECT_ROOT / "data/business/v1"


@pytest.fixture
def case(tmp_path: Path) -> dict:
    path = tmp_path / "app.sqlite3"
    initialize_database(path)
    with transaction(path) as connection:
        for values in json.loads((DATA / "orders.json").read_text(encoding="utf-8")):
            BusinessRepository(connection).add_order(Order.model_validate(values))
    rules = BusinessRules.from_file(DATA / "spec.json")
    tasks = TaskService(path, business_time=rules.business_time)
    task = tasks.create(TaskCreate(user_message="refund damaged order", order_id="ORD-1001"), owner_id="demo-user")
    attempt = tasks.start(task.task_id, owner_id="demo-user")
    action = RefundAction(order_id="ORD-1001", amount_minor=12900, reason="verified damage",
                          policy_refs=(rules.reference("request_refund"),))
    identity = AttemptIdentity(**{name: getattr(attempt, name) for name in AttemptIdentity.model_fields})
    proposal = tasks.propose(action, identity, expires_at=rules.business_time + timedelta(hours=1))
    approval = ApprovalService(path, business_time=rules.business_time).record(task.task_id, ApprovalRequest(
        request_id="confirmation-1", proposal_id=proposal.proposal_id,
        proposal_version=proposal.proposal_version, decision="approved",
    ), owner_id="demo-user")
    context = ExecutionContext(**identity.model_dump(), business_time=rules.business_time, write_binding=WriteBinding(
        proposal_id=proposal.proposal_id, proposal_version=proposal.proposal_version,
        approval_request_id=approval.request.request_id, operation_key="refund-original",
    ))
    business = BusinessService(path, rules)
    business.prepare(action, context)
    return {"path": path, "rules": rules, "action": action, "context": context, "business": business,
            "parameters": local_server_parameters(database=path, data_dir=DATA, cwd=tmp_path)}



def test_real_handshake_catalog_and_seven_tool_roundtrips_leave_database_unchanged(case: dict) -> None:
    before = case["path"].read_bytes()

    async def run():
        async with open_tool_session(case["parameters"]) as client:
            assert client.initialization.protocolVersion == "2025-11-25"
            assert client.initialization.serverInfo.name == "tool-agent-lab"
            tools = await client.list_tools()
            assert [item.model_dump(mode="json", exclude_none=True) for item in tools] == tool_definitions()
            order = await client.call_tool("get_order", {"order_id": "ORD-1001"}, call_id="order-1")
            assert_reply(order, "order-1")
            assert order.result.data.paid_amount_minor == 12900
            assert order.result.data.refunded_amount_minor == 0
            search = await client.call_tool("search_policy", {"query": "延迟补偿券固定500分", "limit": 1},
                                           call_id="search-1")
            assert_reply(search, "search-1")
            reference = search.result.data.hits[0].reference
            assert (reference.policy_id, reference.section) == ("P-DELAY-AMOUNT", "amount")
            read = await client.call_tool("read_policy", {"policy_id": reference.policy_id,
                                          "version": reference.version, "section": reference.section}, call_id="read-1")
            assert_reply(read, "read-1")
            assert read.result.data.text == search.result.data.hits[0].excerpt
            lookup = await client.call_tool("get_operation", {"operation_key": "refund-original"}, call_id="lookup-1")
            assert_reply(lookup, "lookup-1")
            assert lookup.result.data.operation.status == "pending"
            assert lookup.result.data.operation.amount_minor == 12900
            for name in ["request_refund", "issue_coupon"]:
                arguments = case["action"].model_dump(mode="json", exclude={"action"})
                if name == "issue_coupon":
                    arguments.update(amount_minor=500, policy_refs=[case["rules"].reference(name).model_dump(mode="json")])
                assert_reply(await client.call_tool(name, arguments, call_id=name), name,
                             error="execution_context_required")
            handoff = {"order_id": "ORD-1003", "reason": "unsupported_category", "summary": "数字商品转人工",
                       "policy_refs": [case["rules"].reference("create_handoff").model_dump(mode="json")]}
            assert_reply(await client.call_tool("create_handoff", handoff, call_id="handoff-1"), "handoff-1",
                         error="execution_context_required")
    asyncio.run(run())
    assert case["path"].read_bytes() == before


def test_argument_injection_and_failures_are_correlated_and_session_remains_usable(case: dict) -> None:
    async def run():
        async with open_tool_session(case["parameters"]) as client:
            invalid = [
                ("get_order", {}), ("get_order", {"order_id": "ORD-1001", "owner_id": "other-owner"}),
                ("get_order", {"order_id": "ORD-1001", "call_id": "injected"}),
                ("search_policy", {"query": "refund", "limit": True}),
                ("search_policy", {"query": "refund", "limit": 11}),
                ("search_policy", {"query": "refund", "business_time": "2026-08-01T00:00:00+08:00"}),
                ("request_refund", case["action"].model_dump(mode="json", exclude={"action"}) |
                 {"execution_context": case["context"].model_dump(mode="json")}),
                ("request_refund", case["action"].model_dump(mode="json", exclude={"action"}) |
                 {"operation_key": "injected"}),
            ]
            for index, (name, arguments) in enumerate(invalid):
                call_id = f"invalid-{index}"
                assert_reply(await client.call_tool(name, arguments, call_id=call_id), call_id, error="invalid_arguments")
            assert_reply(await client.call_tool("get_order", {"order_id": "missing"}, call_id="missing-order"),
                         "missing-order", error="order_not_found")
            assert_reply(await client.call_tool("read_policy", {
                "policy_id": "P-DELAY-AMOUNT", "version": "mock-policy-v1", "section": "missing",
            }, call_id="missing-section"), "missing-section", error="policy_not_found")
            empty = await client.call_tool("get_operation", {"operation_key": "missing"}, call_id="missing-key")
            assert_reply(empty, "missing-key")
            assert empty.result.data.operation is None
            assert_reply(await client.call_tool("get_order", {"order_id": "ORD-1001"}, call_id="after-errors"), "after-errors")
    asyncio.run(run())


def test_other_user_cannot_read_order_or_ledger_even_with_identity_metadata(case: dict) -> None:
    parameters = local_server_parameters(database=case["path"], data_dir=DATA, owner_id="other-user")

    async def run():
        async with open_tool_session(parameters) as client:
            assert_reply(await client.call_tool("get_order", {"order_id": "ORD-1001"}, call_id="hidden-order"),
                         "hidden-order", error="order_not_found")
            lookup = await client.call_tool("get_operation", {"operation_key": "refund-original"}, call_id="hidden-key")
            assert lookup.result.data.operation is None
            raw = await client.session.call_tool("get_order", {"order_id": "ORD-1001"}, meta={
                "tool_agent_lab_call_id": "fake-identity", "owner_id": "demo-user",
                "execution_context": case["context"].model_dump(mode="json"),
            })
            assert raw.isError and raw.structuredContent["error"]["code"] == "order_not_found"
    asyncio.run(run())


def test_new_session_reads_actual_committed_ledger_after_service_execution(case: dict) -> None:
    case["business"].execute(case["action"], case["context"])

    async def run():
        for index in range(2):
            async with open_tool_session(case["parameters"]) as client:
                lookup = await client.call_tool("get_operation", {"operation_key": "refund-original"},
                                               call_id=f"reconnect-{index}")
                assert lookup.result.data.operation.status == "succeeded"
                assert lookup.result.data.operation.amount_minor == 12900
                order = await client.call_tool("get_order", {"order_id": "ORD-1001"})
                assert order.result.data.refunded_amount_minor == 12900
    asyncio.run(run())
    with connect(case["path"]) as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1


def test_missing_database_returns_error_without_creating_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"

    async def run():
        async with open_tool_session(local_server_parameters(database=path, data_dir=DATA)) as client:
            assert_reply(await client.call_tool("get_order", {"order_id": "ORD-1001"}, call_id="no-db"),
                         "no-db", error="database_not_initialized")
            assert_reply(await client.call_tool("search_policy", {"query": "退款"}, call_id="policy-only"), "policy-only")
    asyncio.run(run())
    assert not path.exists()


def test_wire_uses_json_only_and_rpc_id_fallback_and_exits_on_eof(case: dict, tmp_path: Path) -> None:
    parameters = case["parameters"]
    with (tmp_path / "server.stderr").open("w", encoding="utf-8") as errlog:
        process = subprocess.Popen([parameters.command, *parameters.args], cwd=parameters.cwd,
                                   env={**os.environ, **parameters.env}, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=errlog)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True)
        reader.start()

        def send(message: dict) -> None:
            process.stdin.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
            process.stdin.flush()

        def receive(request_id: str) -> dict:
            response = json.loads(lines.get(timeout=15).decode("utf-8"))
            assert response["jsonrpc"] == "2.0" and response["id"] == request_id
            return response["result"]

        try:
            send({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "wire-test", "version": "1"},
            }})
            assert receive("init")["protocolVersion"] == "2025-11-25"
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": "catalog", "method": "tools/list"})
            assert len(receive("catalog")["tools"]) == 7
            send({"jsonrpc": "2.0", "id": "rpc-call-7", "method": "tools/call",
                  "params": {"name": "unknown_tool", "arguments": {}}})
            result = receive("rpc-call-7")
            assert result["isError"]
            parsed = ToolFailure.model_validate(result["structuredContent"])
            assert parsed.call_id == "rpc-call-7" and parsed.error.code == "unknown_tool"
            process.stdin.close()
            assert process.wait(timeout=15) == 0
            reader.join(timeout=2)
            assert not reader.is_alive() and lines.empty()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=15)
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()


@pytest.mark.parametrize(("command", "input_data", "expected_code"), [
    (["list"], None, 0),
    (["call", "search_policy", "-", "--call-id", "cli-search"], '{"query":"延迟补偿券","limit":1}', 0),
    (["call", "read_policy", '{"policy_id":"P-DELAY-AMOUNT","version":"mock-policy-v1","section":"missing"}'],
     None, 1),
])
def test_cli_utf8_from_other_directory_and_error_exit_codes(
    tmp_path: Path, command: list[str], input_data: str | None, expected_code: int,
) -> None:
    result = subprocess.run([sys.executable, "-m", "tool_agent_lab.tools.client", *command], cwd=tmp_path,
                            env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
                            input=input_data, encoding="utf-8", capture_output=True, timeout=30)
    assert result.returncode == expected_code, result.stderr
    payload = json.loads(result.stdout)
    if command == ["list"]:
        assert len(payload["tools"]) == 7 and payload["protocol_version"] == "2025-11-25"
    elif expected_code == 0:
        assert payload["call_id"] == "cli-search" and payload["status"] == "ok"
    else:
        assert payload["status"] == "error" and payload["error"]["code"] == "policy_not_found"


@pytest.mark.parametrize("bad_result", ["missing_structure", "wrong_call_id", "wrong_error_flag"])
def test_client_rejects_broken_result_identity_or_envelope(bad_result: str) -> None:
    class BrokenSession:
        async def call_tool(self, name, arguments, *, meta):
            payload = {"call_id": "wrong" if bad_result == "wrong_call_id" else meta["tool_agent_lab_call_id"],
                       "status": "error", "error": {"code": "refused", "message": "refused",
                                                  "outcome": "not_committed"}}
            return types.CallToolResult(
                content=[], structuredContent=None if bad_result == "missing_structure" else payload,
                isError=bad_result != "wrong_error_flag",
            )
    client = ToolClient(BrokenSession(), None)
    with pytest.raises(ValueError):
        asyncio.run(client.call_tool("get_order", {"order_id": "ORD-1001"}, call_id="expected"))
