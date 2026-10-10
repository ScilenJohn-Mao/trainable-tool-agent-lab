"""Run the command-line API demo with a separate API, worker and real MCP process."""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from scripts.seed_demo import seed_demo
from tool_agent_lab.settings import PROJECT_ROOT, load_settings
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect


@pytest.fixture
def running_demo(tmp_path):
    runtime = tmp_path / "runtime"
    database = runtime / "app.sqlite3"
    seed_demo(database, settings=load_settings())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("TTAL_")}
    env.update(TTAL_RUNTIME_DIR=str(runtime), TTAL_DEV_OWNER_ID="demo-user",
               TTAL_MODEL_CONFIG_FILE="configs/models/mock.yaml", TTAL_AGENT_CONFIG_FILE="configs/agents/default.yaml")
    processes, logs = [], []
    commands = [
        [sys.executable, "-m", "uvicorn", "apps.api.main:app", "--host", "127.0.0.1", "--port", str(port)],
        [sys.executable, "-m", "apps.worker.main", "--mock-responses", "data/scenarios/demo_refund_replies.json"],
    ]
    try:
        for index, command in enumerate(commands):
            log = (tmp_path / f"service-{index}.log").open("w", encoding="utf-8")
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=PROJECT_ROOT, env=env, stdout=log, stderr=log,
                                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        deadline = time.monotonic() + 15
        with httpx.Client(base_url=base_url, timeout=1) as client:
            while True:
                try:
                    if client.get("/openapi.json").status_code == 200:
                        break
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    pass
                assert processes[0].poll() is None, "API stopped before becoming ready"
                assert time.monotonic() < deadline, "API did not become ready"
                time.sleep(0.1)
        yield {"url": base_url, "database": database, "worker": processes[1], "env": env}
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)
        for log in logs:
            log.close()


@pytest.mark.parametrize("decision", ["approved", "rejected", None])
def test_demo_cli_preserves_version_confirmation_and_actual_ledger(running_demo, tmp_path, decision):
    output = tmp_path / "receipt.json"
    command = [sys.executable, "scripts/demo_agent.py", "--base-url", running_demo["url"], "--output", str(output)]
    if decision:
        command += ["--decision", decision]
    run = subprocess.run(command, cwd=PROJECT_ROOT, env=running_demo["env"], input="", capture_output=True,
                         text=True, encoding="utf-8", timeout=75)
    assert run.returncode == (0 if decision else 1), run.stdout + run.stderr
    report = json.loads(run.stdout)
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert report["model_version"] == "mock-v1" and report["config_version"] == "app-v1"
    assert report["thread_id"] == report["task"]["attempt"]["thread_id"]
    assert report["inputs"][0]["receipt"]["message"] == "ORD-1001"
    assert "waiting_input" in report["states"] and "waiting_approval" in report["states"]
    with connect(running_demo["database"]) as connection:
        order = BusinessRepository(connection).get_order("ORD-1001", "demo-user")
        assert order.refunded_amount_minor == (12900 if decision == "approved" else 0)
        rows = connection.execute("SELECT * FROM operations WHERE task_id=?", (report["task_id"],)).fetchall()
        assert len(rows) == (1 if decision == "approved" else 0)
        approvals = connection.execute("""SELECT a.* FROM approvals a JOIN proposals p USING(proposal_id,proposal_version)
            WHERE p.task_id=?""", (report["task_id"],)).fetchall()
        assert len(approvals) == (1 if decision else 0)
        if decision:
            assert (approvals[0]["consumed_at"] is not None) == (decision == "approved")
    if decision:
        result = report["task"]["result"]
        assert report["finish_reason"] == "completed"
        assert result["outcome"] == ("resolved" if decision == "approved" else "rejected")
        assert len(result["operations"]) == (1 if decision == "approved" else 0)
        if decision == "approved":
            assert result["operations"][0]["amount_minor"] == 12900
        assert result["policy_citations"]
        proposal = report["approvals"][0]["proposal"]
        receipt = report["approvals"][0]["receipt"]
        assert receipt["request"]["proposal_id"] == proposal["proposal_id"]
        assert receipt["request"]["proposal_version"] == proposal["proposal_version"]
        assert receipt["request"]["decision"] == decision and receipt["proposal"] == proposal
    else:
        assert report["finish_reason"] == "operator_input_missing"
        assert report["task"]["status"] == "waiting_approval" and report["approvals"] == []


def test_demo_timeout_preserves_queued_task_without_worker(running_demo, tmp_path):
    running_demo["worker"].terminate()
    running_demo["worker"].wait(timeout=15)
    run = subprocess.run([sys.executable, "scripts/demo_agent.py", "--base-url", running_demo["url"],
                          "--decision", "approved", "--timeout", "0.1"], cwd=PROJECT_ROOT,
                         env=running_demo["env"], capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert run.returncode == 1, run.stderr
    report = json.loads(run.stdout)
    assert report["finish_reason"] == "timeout" and report["task"]["status"] == "queued"
    assert report["inputs"] == [] and report["approvals"] == []
    with httpx.Client(base_url=running_demo["url"]) as client:
        assert client.get(f"/tasks/{report['task_id']}").json()["status"] == "queued"
