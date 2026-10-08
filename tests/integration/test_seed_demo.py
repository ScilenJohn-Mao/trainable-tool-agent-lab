"""Check fresh demo databases, historical receipts and protected execution."""

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import seed_demo as module
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.runtime.task_service import TaskService
from tool_agent_lab.schemas.actions import ApprovalRequest
from tool_agent_lab.schemas.business import Order
from tool_agent_lab.schemas.tasks import AttemptIdentity, TaskCreate
from tool_agent_lab.settings import PROJECT_ROOT, load_settings
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect
from tool_agent_lab.storage.task_repository import TaskRepository
from tool_agent_lab.tools.executor import ToolExecutor

DATA = PROJECT_ROOT / "data/business/v1"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_seed_reconstructs_balances_and_consumed_historical_receipt(tmp_path):
    database = tmp_path / "demo/app.sqlite3"
    report = module.seed_demo(database)
    assert report["status"] == "demo_seeded"
    assert report["business_time"] == "2026-09-17T12:00:00+08:00"
    assert report["order_count"] == 4 and report["historical_operation_count"] == 1
    receipt = report["historical_receipts"][0]
    with connect(database) as connection:
        business = BusinessRepository(connection)
        for row in json.loads((DATA / "orders.json").read_text(encoding="utf-8")):
            assert business.get_order(row["order_id"], row["owner_id"]) == Order.model_validate(row)
        history = business.get_operation("refund-ORD-1003-original", "demo-user")
        assert history.amount_minor == 9900 and history.status == "succeeded"
        assert history.committed_at.isoformat() == "2026-09-17T11:00:00+08:00"
        assert business.get_operation(history.operation_key, "another-user") is None
        tasks = TaskRepository(connection)
        approval = tasks.get_approval(receipt["task_id"], receipt["approval_request_id"], "demo-user")
        assert approval.proposal.proposal_id == receipt["proposal_id"]
        assert approval.proposal.parameters.amount_minor == 9900
        assert approval.decided_at.isoformat() == "2026-09-17T10:59:00+08:00"
        assert tasks.approval_consumed_at(receipt["task_id"], receipt["approval_request_id"], "demo-user") == history.committed_at
        assert tasks.get_task(receipt["task_id"], "demo-user").status == "completed"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
    assert list(database.parent.iterdir()) == [database]


def test_repeat_refuses_and_preserves_added_task_and_database(tmp_path):
    database = tmp_path / "demo.sqlite3"
    module.seed_demo(database)
    rules = BusinessRules.from_file(DATA / "spec.json")
    tasks = TaskService(database, business_time=rules.business_time)
    task = tasks.create(TaskCreate(user_message="new work", order_id="ORD-1001"), owner_id="demo-user")
    before = digest(database)
    with pytest.raises(FileExistsError):
        module.seed_demo(database)
    assert digest(database) == before
    assert tasks.get(task.task_id, owner_id="demo-user") == task


@pytest.mark.parametrize("target", ["configured_app", "configured_checkpoint", "default_app", "default_checkpoint"])
def test_application_and_checkpoint_paths_are_protected_before_any_write(tmp_path, target):
    settings = load_settings().model_copy(update={"runtime_dir": tmp_path / "runtime"})
    paths = {"configured_app": settings.app_db_path, "configured_checkpoint": settings.checkpoint_db_path,
             "default_app": PROJECT_ROOT / "artifacts/runtime/app.sqlite3",
             "default_checkpoint": PROJECT_ROOT / "artifacts/runtime/checkpoints.sqlite3"}
    path = paths[target]
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(ValueError, match="must differ"):
        module.seed_demo(path, settings=settings)
    assert (path.read_bytes() if path.exists() else None) == before


def test_default_path_is_separate_from_configured_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path / "source")
    settings = load_settings().model_copy(update={"runtime_dir": tmp_path / "runtime"})
    report = module.seed_demo(settings=settings)
    assert Path(report["database"]) == tmp_path / "source/artifacts/demo/app.sqlite3"
    assert not settings.runtime_dir.exists()


def test_invalid_fixtures_do_not_create_target(tmp_path):
    settings = load_settings().model_copy(update={"business_data_dir": tmp_path / "missing"})
    target = tmp_path / "output/app.sqlite3"
    with pytest.raises(FileNotFoundError):
        module.seed_demo(target, settings=settings)
    assert not target.parent.exists()


def test_failed_historical_execution_does_not_publish_partial_database(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("injected historical execution failure")
    monkeypatch.setattr(module.BusinessService, "execute", fail)
    target = tmp_path / "output/app.sqlite3"
    with pytest.raises(ValueError, match="injected"):
        module.seed_demo(target)
    assert list(target.parent.iterdir()) == []


def test_cli_from_another_directory_reports_utf8_and_refuses_repeated_target(tmp_path):
    command = [sys.executable, str(PROJECT_ROOT / "scripts/seed_demo.py"), "--database", "demo.sqlite3"]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["database"] == str(tmp_path / "demo.sqlite3")
    with connect(tmp_path / "demo.sqlite3") as connection:
        assert BusinessRepository(connection).get_order("ORD-1001", "demo-user").product_name == "\u9676\u74f7\u9a6c\u514b\u676f"
    before = digest(tmp_path / "demo.sqlite3")
    repeat = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert repeat.returncode == 1 and "already exists" in repeat.stderr
    assert digest(tmp_path / "demo.sqlite3") == before


@pytest.mark.parametrize("name,order_id,amount", [
    ("request_refund", "ORD-1001", 12900), ("issue_coupon", "ORD-1002", 500),
    ("create_handoff", "ORD-1004", None),
])
def test_seeded_database_uses_normal_approval_and_real_mcp(tmp_path, name, order_id, amount):
    database = tmp_path / "demo.sqlite3"
    module.seed_demo(database)
    rules = BusinessRules.from_file(DATA / "spec.json")
    tasks = TaskService(database, business_time=rules.business_time)
    task = tasks.create(TaskCreate(user_message="after sales", order_id=order_id), owner_id="demo-user")
    attempt = tasks.start(task.task_id, owner_id="demo-user")
    identity = AttemptIdentity(**{field: getattr(attempt, field) for field in AttemptIdentity.model_fields})
    executor = ToolExecutor(database, rules, identity, data_dir=DATA)
    values = {"order_id": order_id, "policy_refs": [rules.reference(name).model_dump(mode="json")]}
    values |= {"reason": "unsupported_category", "summary": "manual review"} if amount is None else {
        "reason": "verified facts", "amount_minor": amount,
    }
    original = asyncio.run(executor.call_tool("get_operation", {"operation_key": "refund-ORD-1003-original"}))
    assert original.result.status == "ok" and original.result.data.operation.amount_minor == 9900
    proposal = executor.propose(name, values)
    blocked = asyncio.run(executor.call_tool(name, values))
    assert blocked.result.error.code == "confirmation_required"
    ApprovalService(database, business_time=rules.business_time).record(task.task_id, ApprovalRequest(
        request_id="current-approval", proposal_id=proposal.proposal_id,
        proposal_version=proposal.proposal_version, decision="approved",
    ), owner_id="demo-user")
    result = asyncio.run(executor.call_tool(name, values))
    assert result.result.status == "ok" and result.result.data.status == "succeeded"
    retry = asyncio.run(executor.call_tool(name, values))
    assert retry.result.data == result.result.data
    before = digest(database)
    with pytest.raises(FileExistsError):
        module.seed_demo(database)
    assert digest(database) == before
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 2
        order = BusinessRepository(connection).get_order(order_id, "demo-user")
        assert order.refunded_amount_minor == (12900 if name == "request_refund" else 0)
        assert order.coupon_amount_minor == (500 if name == "issue_coupon" else 0)
