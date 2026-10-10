import asyncio
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from apps.worker.scheduler import WorkerScheduler
from apps.worker.runner import open_worker
from tests.integration.test_worker import environment
from tool_agent_lab.agent.model_client import AssistantMessage, ModelClient
from tool_agent_lab.runtime.leases import LeaseLost, LeaseService
from tool_agent_lab.runtime.task_service import TaskService
from tool_agent_lab.schemas.tasks import TaskCreate
from tool_agent_lab.storage.database import connect

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def setup(tmp_path, *, lease_seconds=30):
    settings, tasks, rules, model, _, _ = environment(tmp_path, [])
    leases = LeaseService(settings.app_db_path, owner_id=settings.dev_owner_id,
                          model_version=model.version, config_version="app-v1",
                          business_time=rules.business_time, lease_seconds=lease_seconds)
    task = tasks.create(TaskCreate(user_message="Please read the request"), owner_id=settings.dev_owner_id)
    return settings, tasks, leases, task, model


def row(settings, task):
    with connect(settings.app_db_path) as connection:
        return dict(connection.execute("SELECT * FROM attempts WHERE attempt_id=?",
                                       (task.current_attempt_id,)).fetchone())


def test_atomic_claim_limits_two_competing_connections_to_one_execution(tmp_path):
    settings, tasks, leases, task, model = setup(tmp_path)
    second = tasks.create(TaskCreate(user_message="Next request"), owner_id=task.owner_id)
    other = LeaseService(settings.app_db_path, owner_id=task.owner_id, model_version=model.version,
                         config_version="app-v1", business_time=leases.business_time)
    barrier = Barrier(2)

    def claim(service):
        barrier.wait(timeout=5)
        return service.claim(now=NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (leases, other)))
    assert sum(result is not None for result in results) == 1
    claimed = next(result for result in results if result)
    assert claimed.execution_epoch == 1 and claimed.attempt.status == "running"
    assert sorted(tasks.get(t.task_id, owner_id=t.owner_id).status for t in (task, second)) == ["queued", "running"]
    stored = row(settings, next(t for t in (task, second) if t.task_id == claimed.attempt.task_id))
    assert stored["lease_worker_id"] == claimed.worker_id
    assert datetime.fromisoformat(stored["heartbeat_at"]) == NOW
    assert datetime.fromisoformat(stored["lease_expires_at"]) == NOW + timedelta(seconds=30)


def test_heartbeat_deadline_timezone_and_expired_query_are_read_only(tmp_path):
    settings, tasks, leases, task, _ = setup(tmp_path)
    lease = leases.claim(now=NOW.astimezone(timezone(timedelta(hours=8))))
    renewed = leases.heartbeat(lease, now=NOW + timedelta(seconds=10))
    assert renewed.lease_expires_at == NOW + timedelta(seconds=40)
    assert leases.expired(now=NOW + timedelta(seconds=39)) == []
    before = row(settings, task)
    assert leases.expired(now=NOW + timedelta(seconds=40)) == [renewed]
    with pytest.raises(LeaseLost):
        leases.heartbeat(lease, now=NOW + timedelta(seconds=40))
    assert row(settings, task) == before
    assert tasks.get(task.task_id, owner_id=task.owner_id).status == "running"
    assert leases.claim(task_id=task.task_id, now=NOW + timedelta(seconds=41)) is None
    assert leases.claim(now=NOW + timedelta(seconds=41)) is None


@pytest.mark.parametrize("change", [{"worker_id": "another-worker"}, {"execution_epoch": 0}])
def test_wrong_worker_or_epoch_cannot_renew_or_release(tmp_path, change):
    settings, _, leases, task, _ = setup(tmp_path)
    lease = leases.claim(now=NOW)
    before = row(settings, task)
    stale = replace(lease, **change)
    with pytest.raises(LeaseLost):
        leases.heartbeat(stale, now=NOW + timedelta(seconds=1))
    assert not leases.release(stale)
    assert row(settings, task) == before


def test_release_and_reclaim_preserve_attempt_and_increase_epoch(tmp_path):
    settings, _, leases, task, _ = setup(tmp_path)
    first = leases.claim(now=NOW)
    assert leases.release(first)
    assert not leases.release(first)
    assert row(settings, task)["execution_epoch"] == 1
    second = leases.claim(task_id=task.task_id, now=NOW + timedelta(seconds=1))
    assert second.attempt == first.attempt
    assert second.execution_epoch == 2
    with pytest.raises(LeaseLost):
        leases.heartbeat(first, now=NOW + timedelta(seconds=2))
    assert not leases.release(first)
    assert leases.release(second)


def test_claim_and_expired_filter_owner_and_versions(tmp_path):
    settings, tasks, leases, task, model = setup(tmp_path)
    leases.claim(now=NOW)
    for owner, model_version, config_version in (
        ("other", model.version, "app-v1"), (task.owner_id, "other", "app-v1"),
        (task.owner_id, model.version, "other"),
    ):
        other = LeaseService(settings.app_db_path, owner_id=owner, model_version=model_version,
                             config_version=config_version, business_time=leases.business_time)
        assert other.expired(now=NOW + timedelta(seconds=30)) == []
    assert len(leases.expired(now=NOW + timedelta(seconds=30))) == 1
    tasks.create(TaskCreate(user_message="Other user"), owner_id="other")
    TaskService(settings.app_db_path, business_time=leases.business_time, model_version="other").create(
        TaskCreate(user_message="Other version"), owner_id=task.owner_id)
    assert leases.claim(now=NOW + timedelta(seconds=30)) is None


def test_lease_time_validation_and_clock_cannot_move_backwards(tmp_path):
    settings, _, leases, task, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="timezone"):
        leases.claim(now=NOW.replace(tzinfo=None))
    first = leases.claim(now=NOW)
    before = row(settings, task)
    with pytest.raises(LeaseLost):
        leases.heartbeat(first, now=NOW - timedelta(seconds=1))
    assert row(settings, task) == before


def test_scheduler_renews_slow_operation_and_releases_on_completion(tmp_path):
    settings, _, leases, task, _ = setup(tmp_path, lease_seconds=1)
    scheduler = WorkerScheduler(leases, heartbeat_interval=0.05)

    async def run():
        lease = leases.claim()
        renewed = asyncio.Event()

        async def operation():
            for _ in range(100):
                if row(settings, task)["heartbeat_at"] != lease.heartbeat_at.isoformat():
                    renewed.set()
                    return "finished"
                await asyncio.sleep(0.01)
            pytest.fail("No heartbeat while execution was active")

        assert await scheduler.execute(lease, operation) == "finished"
        assert renewed.is_set()

    asyncio.run(run())
    assert row(settings, task)["lease_worker_id"] is None
    assert row(settings, task)["execution_epoch"] == 1


@pytest.mark.parametrize("ending", ["error", "cancel", "lease_lost"])
def test_scheduler_stops_execution_and_cleans_up(tmp_path, monkeypatch, ending):
    settings, _, leases, task, _ = setup(tmp_path, lease_seconds=1)
    scheduler = WorkerScheduler(leases, heartbeat_interval=0.02)
    if ending == "lease_lost":
        def lost(*args, **kwargs):
            raise LeaseLost("Lost for the test")
        monkeypatch.setattr(leases, "heartbeat", lost)

    async def run():
        started, stopped = asyncio.Event(), asyncio.Event()

        async def operation():
            started.set()
            try:
                if ending == "error":
                    raise ValueError("Execution failed")
                await asyncio.Event().wait()
            finally:
                stopped.set()

        execution = asyncio.create_task(scheduler.execute(leases.claim(), operation))
        await started.wait()
        if ending == "cancel":
            execution.cancel()
        with pytest.raises({"error": ValueError, "cancel": asyncio.CancelledError, "lease_lost": LeaseLost}[ending]):
            await asyncio.wait_for(execution, timeout=2)
        assert stopped.is_set()

    asyncio.run(run())
    assert row(settings, task)["lease_worker_id"] is None


def test_concurrent_run_once_calls_queue_behind_single_execution(tmp_path, monkeypatch):
    settings, tasks, _, first, model = setup(tmp_path)
    second = tasks.create(TaskCreate(user_message="Another request"), owner_id=first.owner_id)
    original = ModelClient.generate
    active, peak = 0, 0

    async def slow_generate(client, *args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.08)
            return await original(client, *args, **kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(ModelClient, "generate", slow_generate)

    async def run():
        async with open_worker(settings, ModelClient(model),
                               mock_responses=[AssistantMessage(content="Read the request.")],
                               lease_seconds=1, heartbeat_interval=0.02) as worker:
            results = await asyncio.gather(worker.run_once(), worker.run_once())
            assert {r["task_id"] for r in results} == {first.task_id, second.task_id}
            assert all(r["status"] == "completed" for r in results)
            assert await worker.run_once() is None

    asyncio.run(run())
    assert peak == 1
    assert all(row(settings, task)["lease_worker_id"] is None for task in (first, second))


def test_interrupted_worker_leaves_identifiable_lease_without_automatic_replay(tmp_path):
    settings, tasks, rules, model, command, env = environment(tmp_path, [])
    task = tasks.create(TaskCreate(user_message="Interrupted request"), owner_id=settings.dev_owner_id)
    code = """
import asyncio, sys
from apps.worker.runner import open_worker
from tool_agent_lab.settings import load_settings
from tool_agent_lab.agent.model_client import ModelClient, load_model_config
async def paused(self, *args, **kwargs):
    print('generating', flush=True)
    await asyncio.Event().wait()
ModelClient.generate = paused
async def run():
    async with open_worker(load_settings(sys.argv[1]), ModelClient(load_model_config(sys.argv[2])),
                           lease_seconds=1, heartbeat_interval=0.1) as worker:
        await worker.run_once()
asyncio.run(run())
"""
    holder = subprocess.Popen([sys.executable, "-c", code, command[4], command[6]], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "generating"
        active = row(settings, task)
        assert active["lease_worker_id"] and active["execution_epoch"] == 1
        holder.terminate()
        holder.communicate(timeout=10)
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.communicate(timeout=10)
    persisted = row(settings, task)
    leases = LeaseService(settings.app_db_path, owner_id=task.owner_id, model_version=model.version,
                          config_version="app-v1", business_time=rules.business_time)
    deadline = datetime.fromisoformat(persisted["lease_expires_at"])
    assert [lease.attempt.task_id for lease in leases.expired(now=deadline)] == [task.task_id]

    async def poll():
        async with open_worker(settings, ModelClient(model)) as worker:
            assert await worker.run_once() is None

    asyncio.run(poll())
    assert row(settings, task) == persisted
    assert tasks.get(task.task_id, owner_id=task.owner_id).status == "running"


@pytest.mark.parametrize("interval", [0, -1, 30, float("inf"), float("nan")])
def test_scheduler_rejects_invalid_heartbeat_interval(tmp_path, interval):
    _, _, leases, _, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="heartbeat_interval"):
        WorkerScheduler(leases, heartbeat_interval=interval)
