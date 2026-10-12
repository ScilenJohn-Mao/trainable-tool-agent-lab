"""Resume a killed worker on persistent SQLite checkpoints and the original operation key."""

import asyncio
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from apps.worker.runner import open_worker
from tests.integration.test_agent_graph import tool_message
from tests.integration.test_worker import environment
from tool_agent_lab.agent.model_client import AssistantMessage, ModelClient
from tool_agent_lab.agent.inputs import InputRequest, InputService
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.runtime.leases import LeaseService
from tool_agent_lab.runtime.task_service import TaskError
from tool_agent_lab.schemas.actions import ApprovalRequest
from tool_agent_lab.schemas.tasks import TaskCreate
from tool_agent_lab.storage.database import connect
from tool_agent_lab.storage.task_repository import TaskRepository
from tool_agent_lab.tools.executor import ToolExecutor


CRASH_WORKER = """
import asyncio, json, sys, time
from pathlib import Path
from pydantic import TypeAdapter
from apps.worker.runner import open_worker
from tool_agent_lab.agent.nodes import AgentNodes
from tool_agent_lab.agent.graph import Agent
from tool_agent_lab.agent.model_client import AssistantMessage, ModelClient, load_model_config
from tool_agent_lab.settings import load_settings
from tool_agent_lab.tools.executor import ToolExecutor
point = sys.argv[4]
def stop():
    print('paused', flush=True)
    time.sleep(60)
if point == 'before_prepare':
    original = AgentNodes.execute
    async def pause(self, state):
        stop()
        return await original(self, state)
    AgentNodes.execute = pause
elif point == 'after_prepare':
    original = ToolExecutor._prepare
    def pause(self, action):
        result = original(self, action)
        stop()
        return result
    ToolExecutor._prepare = pause
elif point in ('after_commit', 'after_result_event'):
    original = AgentNodes.record_tool_result
    def pause(self, state, result):
        updates = original(self, state, result) if point == 'after_result_event' else None
        stop()
        return updates if updates is not None else original(self, state, result)
    AgentNodes.record_tool_result = pause
elif point == 'after_node_checkpoint':
    original = AgentNodes.decide
    async def pause(self, state):
        if state['operations']:
            stop()
        return await original(self, state)
    AgentNodes.decide = pause
elif point == 'finish_gap':
    original = AgentNodes.finish
    def pause(self, state):
        updates = original(self, state)
        stop()
        return updates
    AgentNodes.finish = pause
elif point == 'after_finish_checkpoint':
    original = Agent.resume
    async def pause(self, *args, **kwargs):
        updates = await original(self, *args, **kwargs)
        stop()
        return updates
    Agent.resume = pause
async def run():
    replies = TypeAdapter(tuple[AssistantMessage, ...]).validate_json(Path(sys.argv[3]).read_text(encoding='utf-8'))
    async with open_worker(load_settings(sys.argv[1]), ModelClient(load_model_config(sys.argv[2])),
                           mock_responses=replies, lease_seconds=5, heartbeat_interval=0.1) as worker:
        await worker.run_once()
asyncio.run(run())
"""


def business_snapshot(settings):
    with connect(settings.app_db_path) as connection:
        return tuple([tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                     for table in ("orders", "approvals", "operations"))


def attempt_row(settings, task):
    with connect(settings.app_db_path) as connection:
        return dict(connection.execute("SELECT * FROM attempts WHERE attempt_id=?",
                                       (task.current_attempt_id,)).fetchone())


def setup(tmp_path, *, refund=False):
    settings, tasks, rules, model, command, env = environment(tmp_path, [])
    replies = [AssistantMessage(content="No further action.")]
    if refund:
        replies.insert(0, tool_message("request_refund", {
            "order_id": "ORD-1001", "amount_minor": 12900, "reason": "Verified damage",
            "policy_refs": [rules.reference("request_refund").model_dump(mode="json")],
        }, "refund-call"))
        (tmp_path / "replies.json").write_text(json.dumps([r.as_message() for r in replies]), encoding="utf-8")
    task = tasks.create(TaskCreate(user_message="Damaged ORD-1001", order_id="ORD-1001"), owner_id=settings.dev_owner_id)
    return settings, tasks, rules, model, command, env, task, replies


def interrupt_refund(tmp_path, point):
    settings, tasks, rules, model, command, env, task, replies = setup(tmp_path, refund=True)
    paused = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert paused.returncode == 0, paused.stderr
    waiting = json.loads(paused.stdout)
    assert waiting["status"] == "waiting_approval"
    proposal = tasks.current_proposal(task.task_id, owner_id=task.owner_id)
    ApprovalService(settings.app_db_path, business_time=rules.business_time).record(task.task_id, ApprovalRequest(
        request_id="human-approval", proposal_id=proposal.proposal_id,
        proposal_version=proposal.proposal_version, decision="approved",
    ), owner_id=task.owner_id)
    holder = subprocess.Popen([sys.executable, "-u", "-c", CRASH_WORKER, command[4], command[6], command[8], point],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        marker = holder.stdout.readline().strip()
        if marker != "paused":
            _, stderr = holder.communicate(timeout=10)
            pytest.fail(f"Worker exited before crash point {point}: {stderr}")
        holder.terminate()
        holder.communicate(timeout=10)
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.communicate(timeout=10)
    row = attempt_row(settings, task)
    assert row["execution_epoch"] == 2 and row["lease_worker_id"] is not None
    deadline = datetime.fromisoformat(row["lease_expires_at"])
    time.sleep(max(0, (deadline - datetime.now(timezone.utc)).total_seconds()) + 0.02)
    return settings, tasks, model, command, env, task, replies, waiting


@pytest.mark.parametrize("point", ["before_prepare", "after_prepare", "after_commit", "after_result_event",
                                   "after_node_checkpoint", "finish_gap", "after_finish_checkpoint"])
def test_killed_worker_recovers_original_thread_without_replaying_committed_write(tmp_path, monkeypatch, point):
    settings, tasks, model, _, _, task, replies, waiting = interrupt_refund(tmp_path, point)
    with connect(settings.app_db_path) as connection:
        before = [dict(r) for r in connection.execute("SELECT * FROM operations WHERE task_id=?", (task.task_id,))]
    if point not in ("before_prepare", "after_prepare"):
        original = ToolExecutor.call_tool

        async def refuse_replay(self, name, *args, **kwargs):
            assert name != "request_refund", "A committed write must be restored from the ledger"
            return await original(self, name, *args, **kwargs)

        monkeypatch.setattr(ToolExecutor, "call_tool", refuse_replay)

    async def recover():
        async with open_worker(settings, ModelClient(model), mock_responses=replies) as worker:
            result = await worker.recover(task.task_id)
            saved = await worker._agent().snapshot(task.task_id, owner_id=task.owner_id)
            assert not saved.next and len(saved.values["operations"]) == 1
            assert saved.values["model_calls"] == 2
            assert len(saved.values["approval_receipts"]) == 1
            assert len([m for m in saved.values["messages"] if m["role"] == "tool"]) == 1
            assert "execution_lease" not in saved.values
            assert await worker.recover(task.task_id) == result
            assert await worker.run_once() is None
            return result

    result = asyncio.run(recover())
    assert result["status"] == tasks.get(task.task_id, owner_id=task.owner_id).status == "completed"
    assert result["thread_id"] == waiting["thread_id"]
    assert result["result"]["outcome"] == "resolved"
    with connect(settings.app_db_path) as connection:
        operations = [dict(r) for r in connection.execute("SELECT * FROM operations WHERE task_id=?", (task.task_id,))]
        assert len(operations) == 1 and operations[0]["status"] == "succeeded"
        assert operations[0]["amount_minor"] == 12900
        if before:
            assert operations[0]["operation_key"] == before[0]["operation_key"]
        assert connection.execute("SELECT refunded_amount_minor FROM orders WHERE order_id='ORD-1001'").fetchone()[0] == 12900
        assert TaskRepository(connection).approval_consumed_at(task.task_id, "human-approval", task.owner_id) is not None
        assert connection.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND event_type='tool_result' AND call_id='refund-call'", (task.task_id,)).fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert attempt_row(settings, task)["execution_epoch"] == 3
    assert attempt_row(settings, task)["lease_worker_id"] is None


@pytest.mark.parametrize("kind,decision", [("input", None), ("input", "recorded"), ("approval", None),
                                         ("approval", "approved"), ("approval", "rejected")])
def test_explicit_recovery_preserves_waiting_or_uses_only_existing_human_receipt(tmp_path, kind, decision):
    settings, tasks, rules, model, _, _, task, replies = setup(tmp_path, refund=kind == "approval")
    if kind == "input":
        replies.insert(0, AssistantMessage(content='{"kind":"ask_user","question":"Which order?"}'))

    async def run():
        async with open_worker(settings, ModelClient(model), mock_responses=replies) as worker:
            waiting = await worker.run_once()
            assert waiting["status"] == "waiting_" + kind
            if decision:
                if kind == "input":
                    InputService(settings.app_db_path, business_time=rules.business_time).record(
                        task.task_id, InputRequest(request_id="human-input", input_request_id=waiting["waiting"]["request_id"],
                                                   message="ORD-1001"), owner_id=task.owner_id)
                else:
                    proposal = tasks.current_proposal(task.task_id, owner_id=task.owner_id)
                    ApprovalService(settings.app_db_path, business_time=rules.business_time).record(task.task_id, ApprovalRequest(
                        request_id="human-approval", proposal_id=proposal.proposal_id,
                        proposal_version=proposal.proposal_version, decision=decision,
                    ), owner_id=task.owner_id)
        before = business_snapshot(settings)
        async with open_worker(settings, ModelClient(model), mock_responses=replies) as restarted:
            result = await restarted.recover(task.task_id)
            assert result["thread_id"] == waiting["thread_id"]
            assert result["status"] == ("completed" if decision else waiting["status"])
            if decision == "approved":
                assert result["result"]["operations"][0]["amount_minor"] == 12900
            else:
                assert business_snapshot(settings) == before
            saved = await restarted._agent().snapshot(task.task_id, owner_id=task.owner_id)
            assert len(saved.values["input_receipts"] if kind == "input" else saved.values["approval_receipts"]) == (1 if decision else 0)

    asyncio.run(run())


def test_recovery_cli_reopens_same_thread_and_reads_committed_receipt(tmp_path):
    settings, _, _, command, env, task, _, waiting = interrupt_refund(tmp_path, "after_commit")
    before = business_snapshot(settings)
    finished = subprocess.run(command + ["--recover-task", task.task_id], env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=30)
    assert finished.returncode == 0, finished.stderr
    result = json.loads(finished.stdout)
    assert result["thread_id"] == waiting["thread_id"] and result["result"]["outcome"] == "resolved"
    assert result["result"]["operations"][0]["amount_minor"] == 12900
    assert business_snapshot(settings) == before


def test_crash_after_claim_before_first_checkpoint_starts_same_attempt(tmp_path):
    settings, tasks, rules, model, _, _, task, replies = setup(tmp_path)

    async def run():
        async with open_worker(settings, ModelClient(model), mock_responses=replies) as worker:
            claim = worker.scheduler.leases.claim(now=datetime.now(timezone.utc) - timedelta(minutes=5))
            result = await worker.recover(task.task_id)
            assert result["thread_id"] == claim.attempt.thread_id
            assert result["status"] == "completed"

    asyncio.run(run())
    assert task.current_attempt_id == tasks.get(task.task_id, owner_id=task.owner_id).current_attempt_id
    assert attempt_row(settings, task)["execution_epoch"] == 2


@pytest.mark.parametrize("problem", ["live_lease", "wrong_model", "wrong_owner", "queued", "cancelled", "failed"])
def test_recovery_refuses_ineligible_task_without_changing_business_or_epoch(tmp_path, problem):
    settings, tasks, _, model, _, _, task, replies = setup(tmp_path)

    async def run():
        selected = settings.model_copy(update={"dev_owner_id": "another-owner"}) if problem == "wrong_owner" else settings
        selected_model = model.model_copy(update={"version": "different-version"}) if problem == "wrong_model" else model
        async with open_worker(selected, ModelClient(selected_model), mock_responses=replies) as worker:
            if problem == "live_lease":
                assert worker.claim_next() is not None
            elif problem == "cancelled":
                tasks.cancel(task.task_id, owner_id=task.owner_id)
            elif problem == "failed":
                worker.mock_responses = ()
                with pytest.raises(ValueError, match="Mock replies exhausted"):
                    await worker.run_once()
            before, row = business_snapshot(settings), attempt_row(settings, task)
            with pytest.raises(TaskError):
                await worker.recover(task.task_id)
            assert business_snapshot(settings) == before
            assert attempt_row(settings, task) == row

    asyncio.run(run())


def test_missing_checkpoint_with_committed_ledger_does_not_restart_model(tmp_path):
    settings, _, model, _, _, task, replies, _ = interrupt_refund(tmp_path, "after_commit")
    settings.checkpoint_db_path.replace(settings.checkpoint_db_path.with_suffix(".saved"))
    before, row = business_snapshot(settings), attempt_row(settings, task)

    async def run():
        async with open_worker(settings, ModelClient(model), mock_responses=replies) as worker:
            with pytest.raises(TaskError, match="recovery_checkpoint_missing"):
                await worker.recover(task.task_id)

    asyncio.run(run())
    assert business_snapshot(settings) == before and attempt_row(settings, task) == row


def test_two_recovery_claimants_atomically_fence_old_epoch(tmp_path):
    settings, _, rules, model, _, _, task, _ = setup(tmp_path)

    def service(worker):
        return LeaseService(settings.app_db_path, owner_id=task.owner_id, model_version=model.version,
                            config_version="app-v1", business_time=rules.business_time, worker_id=worker)

    first = service("old-worker").claim(now=datetime.now(timezone.utc) - timedelta(minutes=5))
    barrier = Barrier(2)

    def claim(worker):
        barrier.wait(timeout=5)
        return service(worker).reclaim(task.task_id, first.execution_epoch)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("second-worker", "third-worker")))
    assert sum(r is not None for r in results) == 1
    winner = next(r for r in results if r)
    assert winner.execution_epoch == 2 and winner.attempt.thread_id == first.attempt.thread_id
    assert not service("old-worker").release(first)


def test_old_invocation_error_does_not_fail_newly_reclaimed_attempt(tmp_path):
    settings, tasks, rules, model, _, _, task, _ = setup(tmp_path)

    async def run():
        async with open_worker(settings, ModelClient(model)) as worker:
            first = worker.claim_next()

            async def stale_error():
                assert worker.scheduler.leases.release(first)
                second = LeaseService(settings.app_db_path, owner_id=task.owner_id, model_version=model.version,
                                      config_version="app-v1", business_time=rules.business_time)
                assert second.reclaim(task.task_id, first.execution_epoch) is not None
                raise ValueError("stale invocation")

            with pytest.raises(ValueError, match="stale invocation"):
                await worker._execute(first, worker._agent(), stale_error)

    asyncio.run(run())
    assert tasks.get(task.task_id, owner_id=task.owner_id).status == "running"
    assert attempt_row(settings, task)["execution_epoch"] == 2
    assert attempt_row(settings, task)["lease_worker_id"] is not None
