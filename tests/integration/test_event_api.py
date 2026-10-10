"""Verify persisted SSE feedback and a live API/worker flow over loopback TCP."""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from tests.integration.test_task_api import api, assert_error, create, worker_tick
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.storage.database import connect, transaction
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository


def frames(lines):
    frame = {}
    for line in lines:
        if not line:
            if "data" in frame:
                frame["data"] = json.loads(frame["data"])
                yield frame
            frame = {}
        elif not line.startswith(":"):
            key, value = line.split(":", 1)
            frame[key] = value.lstrip()


def test_terminal_stream_reads_all_persisted_batches_in_sequence_without_writes(api):
    task = create(api, order_id=None)
    with transaction(api["path"]) as connection:
        attempt = TaskRepository(connection).get_attempt(task["task_id"], task["current_attempt_id"], "demo-user")
        for index in range(205):
            EventRepository(connection).append(attempt, "tool_call", api["rules"].business_time,
                call_id=f"call-{index}", payload={"tool": "get_order", "arguments": {"order_id": "ORD-1001"}})
    assert api["client"].post(f"/tasks/{task['task_id']}/cancel").status_code == 200
    before = api["path"].read_bytes()
    response = api["client"].get(f"/tasks/{task['task_id']}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    received = list(frames(response.iter_lines()))
    assert [int(frame["id"]) for frame in received] == list(range(1, 208))
    with connect(api["path"]) as connection:
        stored = EventRepository(connection).list_events(task["task_id"], "demo-user", limit=300)
    assert [frame["data"] for frame in received] == [event.model_dump(mode="json") for event in stored]
    assert [frame["event"] for frame in received] == [event.event_type for event in stored]
    assert received[-1]["data"]["payload"] == {"status": "cancelled"}
    assert api["path"].read_bytes() == before
    newer = api["client"].get(f"/tasks/{task['task_id']}/events?after_seq=205")
    assert [int(frame["id"]) for frame in frames(newer.iter_lines())] == [206, 207]
    assert api["client"].get(f"/tasks/{task['task_id']}/events?after_seq=207").text == ""


def test_sse_foreign_and_missing_tasks_return_404_before_stream_headers(api):
    task = create(api)
    with TestClient(create_app(api["settings"].model_copy(update={"dev_owner_id": "other-user"}))) as other:
        for task_id in (task["task_id"], "missing"):
            response = other.get(f"/tasks/{task_id}/events", headers={"X-Owner-ID": "demo-user"})
            assert_error(response, 404, "task_not_found")
            assert response.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("cursor", ["-1", "invalid"])
def test_invalid_sse_cursor_returns_422(api, cursor):
    task = create(api)
    assert api["client"].get(f"/tasks/{task['task_id']}/events?after_seq={cursor}").status_code == 422


def test_live_tcp_stream_follows_separate_worker_through_input_confirmation_and_refund(api, tmp_path):
    tick = worker_tick(api, tmp_path)
    with socket.socket() as socket_probe:
        socket_probe.bind(("127.0.0.1", 0))
        port = socket_probe.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("TTAL_")}
    env.update(TTAL_RUNTIME_DIR=str(api["settings"].runtime_dir), TTAL_DEV_OWNER_ID="demo-user",
               TTAL_MODEL_CONFIG_FILE=str(api["settings"].model_config_file),
               TTAL_AGENT_CONFIG_FILE=str(api["settings"].agent_config_file))
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "apps.api.main:app", "--host", "127.0.0.1",
                               "--port", str(port), "--log-level", "warning"], cwd=PROJECT_ROOT, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=35) as client:
            deadline = time.monotonic() + 15
            while True:
                try:
                    if client.get("/openapi.json").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                assert server.poll() is None, "API process exited before becoming ready"
                assert time.monotonic() < deadline, "API did not become ready"
                time.sleep(0.1)
            task = client.post("/tasks", json={"user_message": "My item is damaged"}).json()
            path = f"/tasks/{task['task_id']}"
            # Disconnect an active stream before execution; it must not start or cancel the graph.
            with client.stream("GET", path + "/events") as disconnected:
                assert next(disconnected.iter_lines()) == "id: 1"
            assert client.get(path).json()["status"] == "queued"
            seen = []
            with client.stream("GET", path + "/events") as stream:
                assert stream.status_code == 200
                for frame in frames(stream.iter_lines()):
                    seen.append(frame)
                    event = frame["data"]
                    if event["seq"] == 1:
                        assert tick()["status"] == "waiting_input"
                    elif event["event_type"] == "input_requested":
                        prompt = client.get(path).json()["input_request"]
                        reply = {"request_id": "live-input", "input_request_id": prompt["request_id"], "message": "ORD-1001"}
                        assert client.post(path + "/input", json=reply).status_code == 200
                        assert tick()["status"] == "waiting_approval"
                    elif event["event_type"] == "action_proposed":
                        proposal = client.get(path + "/proposal").json()
                        decision = {"request_id": "live-approval", "proposal_id": proposal["proposal_id"],
                                    "proposal_version": proposal["proposal_version"], "decision": "approved"}
                        assert client.post(path + "/approval", json=decision).status_code == 200
                        assert tick()["status"] == "completed"
            assert [int(frame["id"]) for frame in seen] == list(range(1, len(seen) + 1))
            assert seen[-1]["data"]["payload"]["status"] == "completed"
            assert {frame["event"] for frame in seen} >= {"input_requested", "input_received", "action_proposed", "approval_recorded", "tool_call", "tool_result"}
            result = client.get(path).json()["result"]
            assert result["outcome"] == "resolved" and len(result["operations"]) == 1
            assert result["operations"][0]["amount_minor"] == 12900
            assert {frame["data"]["thread_id"] for frame in seen} == {client.get(path).json()["attempt"]["thread_id"]}
            schema = client.get("/openapi.json").json()
            assert "text/event-stream" in schema["paths"]["/tasks/{task_id}/events"]["get"]["responses"]["200"]["content"]
    finally:
        server.terminate()
        server.communicate(timeout=15)
        assert server.returncode is not None
