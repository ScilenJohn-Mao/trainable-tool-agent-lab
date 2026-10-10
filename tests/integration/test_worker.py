import asyncio
import json
import os
import subprocess
import sys

import pytest

from apps.worker.runner import open_worker
from scripts.seed_demo import seed_demo
from tests.integration.test_agent_graph import tool_message
from tool_agent_lab.agent.inputs import InputRequest, InputService
from tool_agent_lab.agent.model_client import AssistantMessage, ModelClient, ModelConfig
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.runtime.task_service import TaskService
from tool_agent_lab.schemas.actions import ApprovalRequest
from tool_agent_lab.schemas.tasks import TaskCreate
from tool_agent_lab.settings import Settings, load_settings
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect
from tool_agent_lab.storage.task_repository import TaskRepository


def environment(tmp_path, replies):
    defaults = load_settings()
    settings = Settings(config_version="app-v1", mode="mock", dev_owner_id="demo-user",
                        runtime_dir=tmp_path / "runtime", business_data_dir=defaults.business_data_dir)
    seed_demo(settings.app_db_path, settings=defaults)
    rules = BusinessRules.from_file(settings.business_data_dir / "spec.json")
    model = ModelConfig(provider="mock", name="worker-test", version="worker-test-v1")
    tasks = TaskService(settings.app_db_path, business_time=rules.business_time, model_version=model.version)
    config = tmp_path / "app.yaml"
    config.write_text(settings.model_dump_json(), encoding="utf-8")
    model_path = tmp_path / "model.yaml"
    model_path.write_text(model.model_dump_json(), encoding="utf-8")
    script = tmp_path / "replies.json"
    script.write_text(json.dumps([r.as_message() for r in replies]), encoding="utf-8")
    command = [sys.executable, "-m", "apps.worker.main", "--app-config", str(config),
               "--model-config", str(model_path), "--mock-responses", str(script), "--once"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("TTAL_")}
    return settings, tasks, rules, model, command, env


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_independent_worker_input_approval_and_checkpoint_survive_process_exit(tmp_path, decision):
    rules = BusinessRules.from_file(load_settings().business_data_dir / "spec.json")
    replies = [AssistantMessage(content='{"kind":"ask_user","question":"Which order?"}'),
               tool_message("get_order", {"order_id": "ORD-1001"}, "read-order"),
               tool_message("request_refund", {"order_id": "ORD-1001", "amount_minor": 12900,
                   "reason": "Verified damage", "policy_refs": [rules.reference("request_refund").model_dump(mode="json")]}, "refund"),
               AssistantMessage(content="Use the receipt.")]
    settings, tasks, rules, _, command, env = environment(tmp_path, replies)
    task = tasks.create(TaskCreate(user_message="My item is damaged"), owner_id=settings.dev_owner_id)

    def tick():
        finished = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert finished.returncode == 0, finished.stderr
        return json.loads(finished.stdout)

    first = tick()
    assert first["status"] == tasks.get(task.task_id, owner_id=task.owner_id).status == "waiting_input"
    assert tick() == {"status": "idle"}
    input_request = InputRequest(request_id="real-input", input_request_id=first["waiting"]["request_id"], message="ORD-1001")
    inputs = InputService(settings.app_db_path, business_time=rules.business_time)
    inputs.record(task.task_id, input_request, owner_id=task.owner_id)
    second = tick()
    assert second["status"] == "waiting_approval" and second["thread_id"] == first["thread_id"]
    with connect(settings.app_db_path) as connection:
        assert BusinessRepository(connection).get_order("ORD-1001", task.owner_id).refunded_amount_minor == 0
    proposal = tasks.current_proposal(task.task_id, owner_id=task.owner_id)
    request = ApprovalRequest(request_id="real-approval", proposal_id=proposal.proposal_id,
                              proposal_version=proposal.proposal_version, decision=decision)
    approvals = ApprovalService(settings.app_db_path, business_time=rules.business_time)
    approvals.record(task.task_id, request, owner_id=task.owner_id)
    third = tick()
    assert third["status"] == "completed" and third["thread_id"] == first["thread_id"]
    assert third["result"]["outcome"] == ("resolved" if decision == "approved" else "rejected")
    approvals.record(task.task_id, request, owner_id=task.owner_id)
    inputs.record(task.task_id, input_request, owner_id=task.owner_id)
    assert tick() == {"status": "idle"}
    with connect(settings.app_db_path) as connection:
        business = BusinessRepository(connection)
        execution = connection.execute("SELECT execution_epoch, lease_worker_id, lease_expires_at, heartbeat_at FROM attempts WHERE attempt_id=?",
                                       (task.current_attempt_id,)).fetchone()
        assert tuple(execution) == (3, None, None, None)
        assert business.get_order("ORD-1001", task.owner_id).refunded_amount_minor == (12900 if decision == "approved" else 0)
        assert (business.get_successful_operation("ORD-1001", "request_refund", task.owner_id) is not None) == (decision == "approved")
    assert settings.checkpoint_db_path.is_file()


def test_second_process_cannot_claim_and_lock_releases_when_holder_exits(tmp_path):
    settings, tasks, _, _, command, env = environment(tmp_path, [AssistantMessage(content="No action.")])
    task = tasks.create(TaskCreate(user_message="Please read my request"), owner_id=settings.dev_owner_id)
    code = "from pathlib import Path;import sys;from apps.worker.runner import worker_lock\nwith worker_lock(Path(sys.argv[1])):\n print('held',flush=True)\n input()"
    holder = subprocess.Popen([sys.executable, "-c", code, str(settings.app_db_path)], env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        blocked = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
        assert blocked.returncode != 0 and "WorkerBusy" in blocked.stderr
        assert tasks.get(task.task_id, owner_id=task.owner_id).status == "queued"
        holder.communicate("\n", timeout=10)
        released = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
        assert released.returncode == 0, released.stderr
        assert json.loads(released.stdout)["status"] == "completed"
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.communicate(timeout=10)


def test_claim_filters_identity_versions_and_persists_execution_failure_without_retry(tmp_path):
    settings, tasks, rules, model, _, _ = environment(tmp_path, [])
    task = tasks.create(TaskCreate(user_message="No scripted reply exists"), owner_id=settings.dev_owner_id)
    others = [tasks.create(TaskCreate(user_message="Other owner"), owner_id="other-user")]
    for model_version, config_version in (("other-model", "app-v1"), (model.version, "other-config")):
        others.append(TaskService(settings.app_db_path, business_time=rules.business_time, model_version=model_version,
                                  config_version=config_version).create(TaskCreate(user_message="Other version"), owner_id=settings.dev_owner_id))

    async def run():
        async with open_worker(settings, ModelClient(model)) as worker:
            with pytest.raises(ValueError, match="Mock replies exhausted"):
                await worker.run_once()
            assert tasks.get(task.task_id, owner_id=task.owner_id).status == "failed"
            assert await worker.run_once() is None

    asyncio.run(run())
    for other in others:
        assert tasks.get(other.task_id, owner_id=other.owner_id).status == "queued"
    with connect(settings.app_db_path) as connection:
        persisted = TaskRepository(connection).get_attempt(task.task_id, task.current_attempt_id, task.owner_id)
        assert persisted.status == "failed"
        lease = connection.execute("SELECT execution_epoch, lease_worker_id FROM attempts WHERE attempt_id=?",
                                   (task.current_attempt_id,)).fetchone()
        assert tuple(lease) == (1, None)
