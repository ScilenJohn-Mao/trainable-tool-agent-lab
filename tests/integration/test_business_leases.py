"""Fence real MCP writes after a worker claim expires, releases or changes owner."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from mcp_support import DATA, approve, arguments, assert_reply, case
from tool_agent_lab.business.service import BusinessError
from tool_agent_lab.runtime.leases import LeaseService
from tool_agent_lab.schemas.actions import ExecutionLeaseBinding
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect
from tool_agent_lab.tools.client import local_server_parameters, open_tool_session
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS
from tool_agent_lab.tools.executor import ToolExecutor


def lease_service(case, worker_id="worker-first"):
    path, rules, _, identity, _ = case
    return LeaseService(path, owner_id=identity.owner_id, model_version=identity.model_version,
                        config_version=identity.config_version, business_time=rules.business_time,
                        worker_id=worker_id, lease_seconds=120)


def executor_for(case, lease):
    path, rules, _, identity, _ = case
    return ToolExecutor(path, rules, identity, data_dir=DATA, execution_lease=ExecutionLeaseBinding(
        worker_id=lease.worker_id, execution_epoch=lease.execution_epoch,
    ))


def managed(case, name="request_refund", *, now=None):
    _, rules, _, identity, direct = case
    values = arguments(rules, name)
    approve(case, direct.propose(name, values))
    leases = lease_service(case)
    lease = leases.claim(task_id=identity.task_id, now=now)
    assert lease is not None
    return leases, lease, executor_for(case, lease), values


def snapshot(case):
    with connect(case[0]) as connection:
        return tuple([tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                     for table in ("orders", "approvals", "operations"))


def mcp_call(case, context, name, values):
    path, _, _, identity, _ = case

    async def run():
        async with open_tool_session(local_server_parameters(
            database=path, data_dir=DATA, owner_id=identity.owner_id, execution_context=context,
        )) as client:
            return await client.call_tool(name, values, call_id="fenced-write")

    return asyncio.run(run())


@pytest.mark.parametrize("name", ["request_refund", "issue_coupon", "create_handoff"])
def test_live_claim_crosses_real_stdio_and_commits_once(case, name):
    leases, lease, executor, values = managed(case, name)
    result = asyncio.run(executor.call_tool(name, values, call_id="live-write"))
    assert_reply(result, "live-write")
    assert result.result.data.status == "succeeded"
    assert result.result.data.amount_minor == {"request_refund": 12900, "issue_coupon": 500,
                                            "create_handoff": None}[name]
    with connect(case[0]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        assert connection.execute("SELECT consumed_at FROM approvals").fetchone()[0] is not None
    assert leases.release(lease)
    before = snapshot(case)
    retry = asyncio.run(executor.call_tool(name, values, call_id="committed-retry"))
    assert_reply(retry, "committed-retry")
    assert retry.result.data == result.result.data
    assert snapshot(case) == before


@pytest.mark.parametrize("change", ["expired", "released", "new_epoch", "new_worker", "missing_binding"])
@pytest.mark.parametrize("name", ["request_refund", "issue_coupon", "create_handoff"])
def test_claim_changed_after_prepare_is_refused_inside_real_mcp_transaction(case, name, change):
    leases, lease, executor, values = managed(case, name)
    action = TOOL_CONTRACTS[name].parse_arguments(values)
    context = executor._prepare(action)
    if change == "expired":
        # Store an expired deadline through the lease API; the business clock remains fixed in September.
        assert leases.release(lease)
        expired = leases.claim(task_id=case[3].task_id, now=datetime.now(timezone.utc) - timedelta(minutes=5))
        context = context.model_copy(update={"execution_lease": executor_for(case, expired).context.execution_lease})
    elif change in ("released", "new_epoch", "new_worker"):
        assert leases.release(lease)
        if change != "released":
            claimant = leases if change == "new_epoch" else lease_service(case, "worker-second")
            assert claimant.claim(task_id=case[3].task_id).execution_epoch == 2
    else:
        context = context.model_copy(update={"execution_lease": None})
    before = snapshot(case)
    reply = mcp_call(case, context, name, values)
    assert_reply(reply, "fenced-write", error="execution_lease_required" if change == "missing_binding"
                 else "execution_lease_lost")
    assert snapshot(case) == before
    with connect(case[0]) as connection:
        assert connection.execute("SELECT status FROM operations").fetchone()[0] == "pending"
        assert connection.execute("SELECT consumed_at FROM approvals").fetchone()[0] is None


@pytest.mark.parametrize("change", ["expired", "released", "missing_binding", "wrong_worker"])
def test_prepare_cannot_create_pending_operation_without_current_live_claim(case, change):
    now = datetime.now(timezone.utc) - timedelta(minutes=5) if change == "expired" else None
    leases, lease, executor, values = managed(case, now=now)
    if change == "released":
        assert leases.release(lease)
    elif change == "missing_binding":
        executor = case[4]
    elif change == "wrong_worker":
        executor.context = executor.context.model_copy(update={"execution_lease": ExecutionLeaseBinding(
            worker_id="different-worker", execution_epoch=lease.execution_epoch,
        )})
    before = snapshot(case)
    with pytest.raises(BusinessError, match="execution_lease_required" if change == "missing_binding"
                       else "execution_lease_lost"):
        executor._prepare(TOOL_CONTRACTS["request_refund"].parse_arguments(values))
    assert snapshot(case) == before


def test_unbound_context_cannot_bypass_released_managed_attempt(case):
    leases, lease, _, values = managed(case)
    assert leases.release(lease)
    before = snapshot(case)
    reply = asyncio.run(case[4].call_tool("request_refund", values, call_id="unbound-write"))
    assert_reply(reply, "unbound-write", error="execution_lease_required")
    assert snapshot(case) == before


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_same_epoch_deadline_uses_real_utc_and_excludes_equal_endpoint(case, monkeypatch, offset):
    leases, lease, executor, values = managed(case)
    action = TOOL_CONTRACTS["request_refund"].parse_arguments(values)
    context = executor._prepare(action)
    deadline = lease.lease_expires_at

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == timezone.utc
            return deadline + timedelta(microseconds=offset)

    monkeypatch.setattr("tool_agent_lab.business.service.datetime", Clock)
    before = snapshot(case)
    if offset < 0:
        assert executor.business.execute(action, context).amount_minor == 12900
    else:
        with pytest.raises(BusinessError, match="execution_lease_lost"):
            executor.business.execute(action, context)
        assert snapshot(case) == before


def test_live_managed_write_rolls_back_all_business_changes_on_ledger_failure(case, monkeypatch):
    _, _, executor, values = managed(case)
    action = TOOL_CONTRACTS["request_refund"].parse_arguments(values)
    context = executor._prepare(action)
    before = snapshot(case)
    original = BusinessRepository.finish_operation

    def fail(repository, *args, **kwargs):
        assert repository.connection.in_transaction
        assert repository.get_order("ORD-1001", case[3].owner_id).refunded_amount_minor == 12900
        assert repository.connection.execute("SELECT consumed_at FROM approvals").fetchone()[0] is not None
        raise RuntimeError("ledger interrupted")

    monkeypatch.setattr(BusinessRepository, "finish_operation", fail)
    with pytest.raises(RuntimeError, match="ledger interrupted"):
        executor.business.execute(action, context)
    assert snapshot(case) == before
    monkeypatch.setattr(BusinessRepository, "finish_operation", original)
    assert executor.business.execute(action, context).amount_minor == 12900


def test_new_claim_reuses_prepared_key_and_old_claim_can_only_read_committed_result(case):
    leases, first, old_executor, values = managed(case)
    action = TOOL_CONTRACTS["request_refund"].parse_arguments(values)
    original = old_executor._prepare(action)
    assert leases.release(first)
    current = lease_service(case, "worker-second").claim(task_id=case[3].task_id)
    executor = executor_for(case, current)
    reply = asyncio.run(executor.call_tool("request_refund", values, call_id="new-owner"))
    assert_reply(reply, "new-owner")
    result = reply.result.data
    assert result.operation_key == original.write_binding.operation_key
    assert result.amount_minor == 12900
    before = snapshot(case)
    retry = mcp_call(case, original, "request_refund", values)
    assert_reply(retry, "fenced-write")
    assert retry.result.data == result
    lookup = mcp_call(case, original.model_copy(update={"write_binding": None}), "get_operation",
                      {"operation_key": result.operation_key})
    assert_reply(lookup, "fenced-write")
    assert lookup.result.data.operation.model_dump(mode="json") == result.model_dump(mode="json")
    assert snapshot(case) == before
