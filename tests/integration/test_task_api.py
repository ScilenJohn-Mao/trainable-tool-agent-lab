"""Verify HTTP ownership and human decisions against real storage and MCP writes."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from tests.integration.test_agent_graph import tool_message
from tool_agent_lab.agent.model_client import AssistantMessage
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.schemas.business import Order
from tool_agent_lab.schemas.tasks import AttemptIdentity
from tool_agent_lab.settings import PROJECT_ROOT, Settings
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect, initialize_database, transaction
from tool_agent_lab.storage.task_repository import TaskRepository
from tool_agent_lab.tools.executor import ToolExecutor

DATA = PROJECT_ROOT / "data/business/v1"


@pytest.fixture
def api(tmp_path: Path):
    settings = Settings(config_version="app-v1", mode="manual", dev_owner_id="demo-user",
                        runtime_dir=tmp_path, business_data_dir=DATA)
    initialize_database(settings.app_db_path)
    with transaction(settings.app_db_path) as connection:
        for values in json.loads((DATA / "orders.json").read_text(encoding="utf-8")):
            BusinessRepository(connection).add_order(Order.model_validate(values))
    application = create_app(settings)
    with TestClient(application) as client:
        yield {"client": client, "app": application, "settings": settings,
               "path": settings.app_db_path, "rules": BusinessRules.from_file(DATA / "spec.json")}


def create(api, *, order_id="ORD-1001"):
    response = api["client"].post("/tasks", json={"user_message": "refund damaged order", "order_id": order_id})
    assert response.status_code == 201, response.text
    return response.json()


def propose(api, task_id, *, amount=12900, expires_at=None):
    attempt = api["app"].state.tasks.start(task_id, owner_id="demo-user")
    identity = AttemptIdentity(**{field: getattr(attempt, field) for field in AttemptIdentity.model_fields})
    executor = ToolExecutor(api["path"], api["rules"], identity, data_dir=DATA)
    values = {"order_id": "ORD-1001", "amount_minor": amount, "reason": "verified damage",
              "policy_refs": [api["rules"].reference("request_refund").model_dump(mode="json")]}
    return executor, values, executor.propose("request_refund", values, expires_at=expires_at)


def approval_request(proposal, *, decision="approved", request_id="operator-response"):
    return {"request_id": request_id, "proposal_id": proposal.proposal_id,
            "proposal_version": proposal.proposal_version, "decision": decision}


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json() == {"detail": {"code": code}}


def test_create_get_list_and_current_proposal_use_persisted_shared_contracts(api):
    task = create(api)
    assert task["owner_id"] == "demo-user" and task["status"] == "queued"
    assert task["created_at"] == api["rules"].business_time.isoformat()
    assert task["current_attempt_id"]
    client = api["client"]
    detail = client.get(f"/tasks/{task['task_id']}").json()
    assert {key: detail[key] for key in task} == task
    assert detail["input_request"] is None and detail["result"] is None
    assert client.get("/tasks").json() == [task]
    assert client.get(f"/tasks/{task['task_id']}/proposal").json() is None
    with connect(api["path"]) as connection:
        attempt = TaskRepository(connection).get_attempt(task["task_id"], task["current_attempt_id"], "demo-user")
        assert (attempt.status, attempt.model_version, attempt.config_version) == ("queued", "mock-v1", "app-v1")
        assert detail["attempt"] == attempt.model_dump(mode="json")
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    executor, values, proposal = propose(api, task["task_id"])
    assert client.get(f"/tasks/{task['task_id']}").json()["status"] == "waiting_approval"
    assert client.get(f"/tasks/{task['task_id']}/proposal").json() == proposal.model_dump(mode="json")


def test_list_filters_and_pagination_are_owned(api):
    tasks = [create(api, order_id=None) for _ in range(3)]
    api["app"].state.tasks.start(tasks[0]["task_id"], owner_id="demo-user")
    with TestClient(create_app(api["settings"].model_copy(update={"dev_owner_id": "other-user"}))) as other:
        create_other = other.post("/tasks", json={"user_message": "another user"})
        assert create_other.status_code == 201
    client = api["client"]
    all_tasks = client.get("/tasks").json()
    assert len(all_tasks) == 3 and all(item["owner_id"] == "demo-user" for item in all_tasks)
    assert client.get("/tasks", params={"limit": 1, "offset": 1}).json() == all_tasks[1:2]
    assert len(client.get("/tasks", params={"status": "queued"}).json()) == 2
    assert client.get("/tasks", params={"status": "running"}).json()[0]["task_id"] == tasks[0]["task_id"]


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "unknown"}])
def test_invalid_list_query_returns_422(api, query):
    assert api["client"].get("/tasks", params=query).status_code == 422


@pytest.mark.parametrize("extra", [{"owner_id": "other-user"}, {"task_id": "forged"},
                                   {"status": "completed"}, {"execution_context": {}}])
def test_create_rejects_runtime_fields_without_writes(api, extra):
    before = api["path"].read_bytes()
    response = api["client"].post("/tasks", json={"user_message": "new task"} | extra)
    assert response.status_code == 422
    assert api["path"].read_bytes() == before


@pytest.mark.parametrize("message", ["", "   "])
def test_create_rejects_empty_messages(api, message):
    assert api["client"].post("/tasks", json={"user_message": message}).status_code == 422


def test_unknown_or_foreign_orders_and_tasks_have_matching_404_results(api):
    task = create(api)
    executor, values, proposal = propose(api, task["task_id"])
    request = approval_request(proposal)
    client = api["client"]
    assert_error(client.post("/tasks", json={"user_message": "missing", "order_id": "missing"}), 404, "order_not_found")
    with TestClient(create_app(api["settings"].model_copy(update={"dev_owner_id": "other-user"}))) as other:
        before = api["path"].read_bytes()
        for task_id in [task["task_id"], "missing"]:
            assert_error(other.get(f"/tasks/{task_id}"), 404, "task_not_found")
            assert_error(other.get(f"/tasks/{task_id}/proposal"), 404, "task_not_found")
            assert_error(other.post(f"/tasks/{task_id}/approval", json=request,
                                    headers={"X-Owner-ID": "demo-user"}), 404, "task_not_found")
        assert_error(other.post("/tasks", json={"user_message": "foreign", "order_id": "ORD-1001"}),
                     404, "order_not_found")
        assert other.get("/tasks", params={"owner_id": "demo-user"}, headers={"X-Owner-ID": "demo-user"}).json() == []
        assert api["path"].read_bytes() == before


def test_http_headers_and_query_cannot_change_server_identity(api):
    response = api["client"].post("/tasks?owner_id=other-user", json={"user_message": "new task"},
                                    headers={"X-Owner-ID": "other-user"})
    assert response.status_code == 201 and response.json()["owner_id"] == "demo-user"


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_http_decision_controls_real_mcp_write_and_exact_retries(api, decision):
    task = create(api)
    executor, values, proposal = propose(api, task["task_id"])
    client = api["client"]
    before_execution = asyncio.run(executor.call_tool("request_refund", values, call_id="unconfirmed"))
    assert before_execution.result.error.code == "confirmation_required"
    request = approval_request(proposal, decision=decision)
    response = client.post(f"/tasks/{task['task_id']}/approval", json=request)
    assert response.status_code == 200, response.text
    approval = response.json()
    assert approval["proposal"] == proposal.model_dump(mode="json")
    assert approval["owner_id"] == "demo-user" and approval["request"]["decision"] == decision
    assert client.get(f"/tasks/{task['task_id']}").json()["status"] == "running"
    with connect(api["path"]) as connection:
        assert BusinessRepository(connection).get_order("ORD-1001", "demo-user").refunded_amount_minor == 0
        assert TaskRepository(connection).approval_consumed_at(task["task_id"], request["request_id"], "demo-user") is None
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    before = api["path"].read_bytes()
    assert client.post(f"/tasks/{task['task_id']}/approval", json=request).json() == approval
    assert api["path"].read_bytes() == before
    reply = asyncio.run(executor.call_tool("request_refund", values, call_id="after-decision"))
    if decision == "approved":
        assert reply.result.data.status == "succeeded" and reply.result.data.amount_minor == 12900
        assert not reply.raw.isError and reply.raw.structuredContent == reply.result.model_dump(mode="json")
        retry = asyncio.run(executor.call_tool("request_refund", values, call_id="retry"))
        assert retry.result.data == reply.result.data
        lookup = asyncio.run(executor.call_tool("get_operation", {"operation_key": reply.result.data.operation_key}))
        assert lookup.result.data.operation.model_dump(mode="json") == reply.result.data.model_dump(mode="json")
    else:
        assert reply.result.error.code == "confirmation_required"
    after = api["path"].read_bytes()
    assert client.post(f"/tasks/{task['task_id']}/approval", json=request).json() == approval
    assert api["path"].read_bytes() == after
    with connect(api["path"]) as connection:
        assert connection.execute("SELECT count(*) FROM approvals").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == (1 if decision == "approved" else 0)
        assert BusinessRepository(connection).get_order("ORD-1001", "demo-user").refunded_amount_minor == (
            12900 if decision == "approved" else 0
        )


@pytest.mark.parametrize("extra", [{"owner_id": "demo-user"}, {"amount_minor": 12900},
                                   {"execution_context": {}}, {"operation_key": "forged"}])
def test_approval_rejects_identity_parameters_and_binding_injection(api, extra):
    task = create(api)
    executor, values, proposal = propose(api, task["task_id"])
    before = api["path"].read_bytes()
    response = api["client"].post(f"/tasks/{task['task_id']}/approval", json=approval_request(proposal) | extra)
    assert response.status_code == 422
    assert api["path"].read_bytes() == before


@pytest.mark.parametrize("case", ["not_waiting", "stale_version", "expired", "request_conflict", "already_decided"])
def test_approval_service_conflicts_are_409_without_new_writes(api, case):
    task = create(api)
    executor, values, proposal = propose(api, task["task_id"], expires_at=api["rules"].business_time + timedelta(hours=1))
    request = approval_request(proposal)
    if case == "not_waiting":
        task = create(api, order_id=None)
        code = "task_not_waiting_approval"
    elif case == "stale_version":
        executor.propose("request_refund", values | {"reason": "changed reason"})
        code = "proposal_not_current"
    elif case == "expired":
        api["app"].state.approvals = ApprovalService(api["path"], business_time=proposal.expires_at)
        code = "proposal_expired"
    else:
        assert api["client"].post(f"/tasks/{task['task_id']}/approval", json=request).status_code == 200
        if case == "request_conflict":
            request["decision"] = "rejected"
            code = "confirmation_request_conflict"
        else:
            request["request_id"] = "another-request"
            code = "proposal_already_decided"
    before = api["path"].read_bytes()
    assert_error(api["client"].post(f"/tasks/{task['task_id']}/approval", json=request), 409, code)
    assert api["path"].read_bytes() == before


def test_http_approval_cannot_authorize_wrong_amount(api):
    task = create(api)
    executor, values, proposal = propose(api, task["task_id"], amount=1)
    assert api["client"].post(f"/tasks/{task['task_id']}/approval", json=approval_request(proposal)).status_code == 200
    reply = asyncio.run(executor.call_tool("request_refund", values))
    assert reply.result.error.code == "amount_mismatch"
    with connect(api["path"]) as connection:
        assert BusinessRepository(connection).get_order("ORD-1001", "demo-user").refunded_amount_minor == 0
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


def test_http_storage_failure_rolls_back_decision_status_and_events(api):
    task = create(api)
    executor, values, proposal = propose(api, task["task_id"])
    with transaction(api["path"]) as connection:
        connection.execute("""CREATE TRIGGER fail_decision BEFORE INSERT ON events
            WHEN NEW.event_type = 'approval_recorded'
            BEGIN SELECT RAISE(ABORT, 'event storage unavailable'); END""")
    with TestClient(create_app(api["settings"]), raise_server_exceptions=False) as client:
        before = api["path"].read_bytes()
        response = client.post(f"/tasks/{task['task_id']}/approval", json=approval_request(proposal))
        assert response.status_code == 500
        assert api["path"].read_bytes() == before
        assert client.get(f"/tasks/{task['task_id']}").json()["status"] == "waiting_approval"
    with connect(api["path"]) as connection:
        assert connection.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


def test_openapi_exposes_shared_input_contracts(api):
    schema = api["client"].get("/openapi.json").json()
    assert set(schema["paths"]) == {"/tasks", "/tasks/{task_id}", "/tasks/{task_id}/proposal",
                                   "/tasks/{task_id}/approval", "/tasks/{task_id}/input", "/tasks/{task_id}/cancel",
                                   "/tasks/{task_id}/events"}
    for name in ["TaskCreate", "ApprovalRequest", "InputRequest"]:
        assert schema["components"]["schemas"][name]["additionalProperties"] is False
        assert "owner_id" not in schema["components"]["schemas"][name]["properties"]
    assert schema["paths"]["/tasks"]["post"]["responses"]["201"]


def test_lifespan_initializes_empty_database_and_restart_preserves_tasks(tmp_path):
    settings = Settings(config_version="app-v1", mode="manual", dev_owner_id="demo-user",
                        runtime_dir=tmp_path / "runtime", business_data_dir=DATA)
    application = create_app(settings)
    assert not settings.app_db_path.exists()
    with TestClient(application) as client:
        task = client.post("/tasks", json={"user_message": "need order information"}).json()
    with TestClient(create_app(settings)) as client:
        detail = client.get(f"/tasks/{task['task_id']}").json()
        assert {key: detail[key] for key in task} == task
        with connect(settings.app_db_path) as connection:
            assert connection.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def worker_tick(api, tmp_path):
    """Use the same application config for HTTP creation and the separate worker."""
    replies = [AssistantMessage(content='{"kind":"ask_user","question":"Which order?"}'),
               tool_message("get_order", {"order_id": "ORD-1001"}, "read-order"),
               tool_message("request_refund", {"order_id": "ORD-1001", "amount_minor": 12900,
                   "reason": "Verified damage", "policy_refs": [api["rules"].reference("request_refund").model_dump(mode="json")]}, "refund"),
               AssistantMessage(content="Use the receipt.")]
    config = tmp_path / "application.yaml"
    config.write_text(api["settings"].model_dump_json(), encoding="utf-8")
    script = tmp_path / "replies.json"
    script.write_text(json.dumps([r.as_message() for r in replies]), encoding="utf-8")
    command = [sys.executable, "-m", "apps.worker.main", "--app-config", str(config),
               "--mock-responses", str(script), "--once"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("TTAL_")}

    def tick():
        run = subprocess.run(command, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
                             encoding="utf-8", timeout=30)
        assert run.returncode == 0, run.stderr
        return json.loads(run.stdout)
    return tick


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_http_input_and_approval_drive_independent_worker_to_owned_result(api, tmp_path, decision):
    client = api["client"]
    tick = worker_tick(api, tmp_path)
    task = create(api, order_id=None)
    path = f"/tasks/{task['task_id']}"
    first = tick()
    detail = client.get(path).json()
    assert detail["status"] == "waiting_input" and detail["input_request"] == first["waiting"]
    thread_id = detail["attempt"]["thread_id"]
    request = {"request_id": "input-reply", "input_request_id": detail["input_request"]["request_id"], "message": "ORD-1001"}
    assert_error(client.post(path + "/input", json=request | {"input_request_id": "stale"}), 409, "input_request_not_current")
    assert client.post(path + "/input", json=request | {"owner_id": "other"}).status_code == 422
    assert client.post(path + "/input", json=request).json() == request
    before = api["path"].read_bytes()
    assert client.post(path + "/input", json=request).json() == request
    assert api["path"].read_bytes() == before
    assert_error(client.post(path + "/input", json=request | {"message": "changed"}), 409, "input_request_conflict")
    assert_error(client.post(path + "/cancel"), 409, "task_not_cancellable")
    second = tick()
    assert second["status"] == "waiting_approval" and second["thread_id"] == thread_id
    proposal = client.get(path + "/proposal").json()
    approval = {"request_id": "http-decision", "proposal_id": proposal["proposal_id"],
                "proposal_version": proposal["proposal_version"], "decision": decision}
    assert_error(client.post(path + "/approval", json=approval | {"proposal_version": proposal["proposal_version"] + 1}),
                 409, "proposal_not_current")
    receipt = client.post(path + "/approval", json=approval)
    assert receipt.status_code == 200, receipt.text
    third = tick()
    detail = client.get(path).json()
    assert detail["status"] == detail["attempt"]["status"] == "completed"
    assert detail["result"] == third["result"] and third["thread_id"] == thread_id
    assert detail["result"]["outcome"] == ("resolved" if decision == "approved" else "rejected")
    assert detail["input_request"] is None
    assert client.post(path + "/input", json=request).json() == request
    assert client.post(path + "/approval", json=approval).json() == receipt.json()
    assert tick() == {"status": "idle"}
    assert_error(client.post(path + "/cancel"), 409, "task_not_cancellable")
    with connect(api["path"]) as connection:
        assert BusinessRepository(connection).get_order("ORD-1001", "demo-user").refunded_amount_minor == (12900 if decision == "approved" else 0)
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == (1 if decision == "approved" else 0)


@pytest.mark.parametrize("waiting", ["queued", "waiting_input", "waiting_approval"])
def test_http_cancel_waiting_tasks_is_idempotent_and_worker_does_not_resume(api, tmp_path, waiting):
    client = api["client"]
    tick = worker_tick(api, tmp_path)
    task = create(api, order_id=None)
    path = f"/tasks/{task['task_id']}"
    input_request = {"request_id": "late-input", "input_request_id": "unused", "message": "ORD-1001"}
    if waiting != "queued":
        first = tick()
        input_request["input_request_id"] = first["waiting"]["request_id"]
        if waiting == "waiting_approval":
            assert client.post(path + "/input", json=input_request).status_code == 200
            assert tick()["status"] == waiting
    response = client.post(path + "/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    before = api["path"].read_bytes()
    assert client.post(path + "/cancel").json() == response.json()
    assert api["path"].read_bytes() == before
    detail = client.get(path).json()
    assert detail["status"] == detail["attempt"]["status"] == "cancelled"
    if waiting == "waiting_approval":
        proposal = client.get(path + "/proposal").json()
        late = {"request_id": "late-decision", "proposal_id": proposal["proposal_id"],
                "proposal_version": proposal["proposal_version"], "decision": "approved"}
        assert_error(client.post(path + "/approval", json=late), 409, "task_not_waiting_approval")
    else:
        assert_error(client.post(path + "/input", json=input_request), 409, "task_not_waiting_input")
    assert tick() == {"status": "idle"}
    with connect(api["path"]) as connection:
        assert BusinessRepository(connection).get_order("ORD-1001", "demo-user").refunded_amount_minor == 0
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


def test_input_cancel_and_detail_hide_foreign_tasks(api):
    task = create(api)
    with TestClient(create_app(api["settings"].model_copy(update={"dev_owner_id": "other-user"}))) as other:
        before = api["path"].read_bytes()
        for task_id in (task["task_id"], "missing"):
            assert_error(other.get(f"/tasks/{task_id}"), 404, "task_not_found")
            assert_error(other.post(f"/tasks/{task_id}/input", json={"request_id": "x", "input_request_id": "x", "message": "x"}), 404, "task_not_found")
            assert_error(other.post(f"/tasks/{task_id}/cancel"), 404, "task_not_found")
        assert api["path"].read_bytes() == before


def test_cancel_storage_failure_rolls_back_task_attempt_and_event(api):
    task = create(api)
    with transaction(api["path"]) as connection:
        connection.execute("""CREATE TRIGGER fail_cancel BEFORE INSERT ON events
            WHEN json_extract(NEW.payload_json, '$.status') = 'cancelled'
            BEGIN SELECT RAISE(ABORT, 'event storage unavailable'); END""")
    with TestClient(create_app(api["settings"]), raise_server_exceptions=False) as client:
        before = api["path"].read_bytes()
        assert client.post(f"/tasks/{task['task_id']}/cancel").status_code == 500
        assert api["path"].read_bytes() == before
        detail = client.get(f"/tasks/{task['task_id']}").json()
        assert detail["status"] == detail["attempt"]["status"] == "queued"


def test_custom_model_and_agent_files_bind_http_and_worker_versions(api, tmp_path):
    model_file = tmp_path / "model.yaml"
    model_file.write_text(json.dumps({"provider": "mock", "name": "configured", "version": "configured-v2"}), encoding="utf-8")
    agent_file = tmp_path / "agent.yaml"
    agent_file.write_text(json.dumps({"version": "agent-v2", "budgets_path": "configs/budgets.yaml",
                                      "prompt_path": "configs/prompts/after_sales.txt"}), encoding="utf-8")
    settings = api["settings"].model_copy(update={"model_config_file": model_file, "agent_config_file": agent_file})
    with TestClient(create_app(settings)) as client:
        configured = api | {"client": client, "settings": settings}
        task = create(configured, order_id=None)
        outcome = worker_tick(configured, tmp_path)()
        detail = client.get(f"/tasks/{task['task_id']}").json()
        assert outcome["status"] == "waiting_input"
        assert (detail["attempt"]["model_version"], detail["attempt"]["config_version"]) == ("configured-v2", "agent-v2")


def test_api_local_model_configuration_does_not_load_gpu_libraries(api):
    settings = api["settings"].model_copy(update={"model_config_file": PROJECT_ROOT / "configs/models/qwen3b-local.yaml"})
    from tool_agent_lab.agent.model_client import load_model_config
    with TestClient(create_app(settings)) as client:
        task = client.post("/tasks", json={"user_message": "local model task"}).json()
        assert client.get(f"/tasks/{task['task_id']}").json()["attempt"]["model_version"] == load_model_config(settings.model_config_file).version
    assert "torch" not in sys.modules
