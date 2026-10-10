"""Drive browser forms against the real API, independent worker and MCP tools."""

import os
import shutil
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


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def await_ready(url, process):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        assert process.poll() is None, f"Service stopped before ready: {url}"
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError(f"Service did not become ready: {url}")


@pytest.mark.parametrize("decision", ["approved", "rejected", "cancelled"])
def test_browser_ticket_flow_keeps_authorization_amount_and_thread(tmp_path, decision):
    node = shutil.which("node")
    assert node, "Install Node.js and run npm ci in frontend/ first"
    frontend = PROJECT_ROOT / "frontend"
    api_port, frontend_port = free_port(), free_port()
    runtime = tmp_path / "runtime"
    database = runtime / "app.sqlite3"
    seed_demo(database, settings=load_settings())
    env = {k: v for k, v in os.environ.items() if not k.startswith("TTAL_")}
    env.pop("FORCE_COLOR", None)
    env.update(TTAL_RUNTIME_DIR=str(runtime), TTAL_MODEL_CONFIG_FILE="configs/models/mock.yaml",
               TTAL_API_TARGET=f"http://127.0.0.1:{api_port}", TTAL_FRONTEND_URL=f"http://127.0.0.1:{frontend_port}",
               TTAL_BROWSER_DECISION=decision, TTAL_BROWSER_OUTPUT=str(tmp_path / "browser"),
               TTAL_DEV_OWNER_ID="demo-user", TTAL_AGENT_CONFIG_FILE="configs/agents/default.yaml")
    commands = [
        ([sys.executable, "-m", "uvicorn", "apps.api.main:app", "--host", "127.0.0.1", "--port", str(api_port)], PROJECT_ROOT),
        ([sys.executable, "-m", "apps.worker.main", "--mock-responses", "data/scenarios/demo_refund_replies.json"], PROJECT_ROOT),
        ([node, str(frontend / "node_modules/vite/bin/vite.js"), "--host", "127.0.0.1", "--port", str(frontend_port)], frontend),
    ]
    processes, logs = [], []
    try:
        for index, (command, cwd) in enumerate(commands):
            log = (tmp_path / f"service-{index}.log").open("w", encoding="utf-8")
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=log,
                                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        await_ready(f"http://127.0.0.1:{api_port}/openapi.json", processes[0])
        await_ready(env["TTAL_FRONTEND_URL"], processes[2])
        run = subprocess.run([node, str(frontend / "node_modules/@playwright/test/cli.js"), "test"],
                             cwd=frontend, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert run.returncode == 0, run.stdout + run.stderr
        with connect(database) as connection:
            order = BusinessRepository(connection).get_order("ORD-1001", "demo-user")
            assert order.refunded_amount_minor == (12900 if decision == "approved" else 0)
            count = connection.execute("SELECT count(*) FROM operations WHERE order_id='ORD-1001'").fetchone()[0]
            assert count == (1 if decision == "approved" else 0)
            approvals = connection.execute("""SELECT a.consumed_at FROM approvals a
                JOIN proposals p USING (proposal_id, proposal_version)
                JOIN tasks t ON t.task_id=p.task_id
                WHERE t.user_message<> 'Historical approved refund'""").fetchall()
            assert len(approvals) == (0 if decision == "cancelled" else 1)
            if approvals:
                assert (approvals[0][0] is not None) == (decision == "approved")
        assert processes[1].poll() is None, "Worker stopped during browser flow"
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)
        for log in logs:
            log.close()
