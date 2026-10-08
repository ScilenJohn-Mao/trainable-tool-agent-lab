"""Validate the manual refund demo, actual decisions and real MCP business results."""

import asyncio
import hashlib
import json
import subprocess
import sys
from contextlib import asynccontextmanager

import pytest

from scripts import demo_business as module
from tool_agent_lab.settings import PROJECT_ROOT, load_settings
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository


def run_cli(tmp_path, *arguments, answer=""):
    return subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/demo_business.py"),
                           "--database", str(tmp_path / "app.sqlite3"), *arguments],
                          cwd=tmp_path, input=answer, capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_real_refund_demo_finishes_consistent_business_and_event_records(tmp_path, capfd):
    database = tmp_path / "app.sqlite3"
    report = asyncio.run(module.demo_refund(database, decision="approved"))
    displayed = json.loads(capfd.readouterr().err)
    assert displayed["proposal"] == report["proposal"]
    assert displayed["order"]["paid_amount_minor"] == 12900
    assert displayed["policy"]["reference"]["policy_id"].startswith("P-REFUND-")
    assert report["status"] == "refunded" and report["workflow"] == "manual_tools"
    assert report["business_time"] == "2026-09-17T12:00:00+08:00"
    assert report["task"]["status"] == "completed"
    assert report["order_before"]["refunded_amount_minor"] == 0
    assert report["order_after"]["refunded_amount_minor"] == 12900
    assert report["order_after"]["coupon_amount_minor"] == 0
    operation = report["operation"]
    assert operation["amount_minor"] == 12900 and operation["status"] == "succeeded"
    assert report["approval_consumed_at"] == operation["committed_at"]
    assert report["original_key_lookup_matches"] and report["same_key_repeat_matches"]
    calls = report["calls"]
    assert [call["tool"] for call in calls] == ["get_order", "search_policy", "read_policy", "request_refund",
                                               "get_operation", "request_refund", "get_order"]
    assert all(call["result"]["status"] == "ok" and not call["mcp_is_error"] for call in calls)
    with connect(database) as connection:
        repo = TaskRepository(connection)
        task = report["task"]
        assert repo.get_attempt(task["task_id"], task["current_attempt_id"], task["owner_id"]).status == "completed"
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        events = EventRepository(connection).list_events(task["task_id"], task["owner_id"])
        assert [event.seq for event in events] == list(range(1, len(events) + 1))
        assert len([event for event in events if event.event_type == "tool_call"]) == 7
        for call in calls:
            pair = [event for event in events if event.call_id == call["result"]["call_id"]]
            assert [event.event_type for event in pair] == ["tool_call", "tool_result"]
        assert events[-1].payload == {"status": "completed"}


def test_rejection_records_decision_and_finishes_without_a_refund(tmp_path, capfd):
    database = tmp_path / "app.sqlite3"
    report = asyncio.run(module.demo_refund(database, decision="rejected"))
    assert report["status"] == "rejected" and report["task"]["status"] == "completed"
    assert report["order_after"] == report["order_before"]
    assert report["operation"] is None and report["approval_consumed_at"] is None
    assert not report["original_key_lookup_matches"] and not report["same_key_repeat_matches"]
    assert not any(call["tool"] == "request_refund" for call in report["calls"])
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
        approval = report["approval"]
        persisted = TaskRepository(connection).get_approval(report["task"]["task_id"], approval["request"]["request_id"], "demo-user")
        assert persisted.request.decision == "rejected"


@pytest.mark.parametrize("decision,status,amount", [("approved", "refunded", 12900), ("rejected", "rejected", 0)])
def test_cli_reads_actual_operator_input_and_outputs_utf8_json(tmp_path, decision, status, amount):
    result = run_cli(tmp_path, answer=decision + "\n")
    assert result.returncode == 0, result.stderr
    assert "Enter approved or rejected" in result.stderr
    assert "\u9676\u74f7\u9a6c\u514b\u676f" in result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == status and report["order_after"]["refunded_amount_minor"] == amount
    assert report["approval"]["request"]["decision"] == decision
    assert report["database"] == str(tmp_path / "app.sqlite3")


def test_explicit_cli_decision_and_repeated_target_refusal(tmp_path):
    result = run_cli(tmp_path, "--decision", "approved")
    assert result.returncode == 0, result.stderr
    assert "Enter approved or rejected" not in result.stderr
    assert json.loads(result.stdout)["operation"]["amount_minor"] == 12900
    database = tmp_path / "app.sqlite3"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    repeated = run_cli(tmp_path, "--decision", "approved")
    assert repeated.returncode == 1 and "already exists" in repeated.stderr
    assert repeated.stdout == "" and hashlib.sha256(database.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("answer", ["", "yes\n"])
def test_missing_or_invalid_operator_decision_never_approves_or_writes(tmp_path, answer):
    result = run_cli(tmp_path, answer=answer)
    assert result.returncode == 1 and result.stdout == ""
    with connect(tmp_path / "app.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM approvals").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
        task = connection.execute("SELECT status FROM tasks WHERE order_id='ORD-1001'").fetchone()
        assert task["status"] == "waiting_approval"
        assert BusinessRepository(connection).get_order("ORD-1001", "demo-user").refunded_amount_minor == 0


def test_configured_application_database_is_rejected_without_creating_it(tmp_path):
    settings = load_settings().model_copy(update={"runtime_dir": tmp_path / "runtime"})
    with pytest.raises(ValueError, match="must differ"):
        asyncio.run(module.demo_refund(settings.app_db_path, decision="approved", settings=settings))
    assert not settings.runtime_dir.exists()


def test_transport_failure_leaves_prepared_key_unconsumed_and_task_unfinished(tmp_path, monkeypatch, capfd):
    import tool_agent_lab.tools.executor as executor_module
    original_open = executor_module.open_tool_session

    @asynccontextmanager
    async def fail_write(parameters):
        context = json.loads(parameters.env["TTAL_MCP_CONTEXT"])
        if context.get("write_binding"):
            raise RuntimeError("injected transport failure")
        async with original_open(parameters) as client:
            yield client

    monkeypatch.setattr(executor_module, "open_tool_session", fail_write)
    database = tmp_path / "app.sqlite3"
    with pytest.raises(RuntimeError, match="injected transport failure"):
        asyncio.run(module.demo_refund(database, decision="approved"))
    with connect(database) as connection:
        pending = connection.execute("SELECT * FROM operations WHERE status='pending'").fetchone()
        assert pending is not None and pending["amount_minor"] == 12900
        assert BusinessRepository(connection).get_order("ORD-1001", "demo-user").refunded_amount_minor == 0
        tasks = TaskRepository(connection)
        assert tasks.approval_consumed_at(pending["task_id"], pending["approval_request_id"], "demo-user") is None
        assert tasks.get_task(pending["task_id"], "demo-user").status == "running"
