import asyncio
import json
from argparse import Namespace
from datetime import timedelta

import pytest
from pydantic import ValidationError

from scripts.demo_simulation import run as run_demo, scripted_replies
from tool_agent_lab.agent.config import load_agent_config
from tool_agent_lab.agent.model_client import ModelClient, ModelConfig
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.schemas.actions import ApprovalRequest
from tool_agent_lab.schemas.scenarios import ExperimentConfig, Scenario, UserBehaviour
from tool_agent_lab.settings import load_settings
from tool_agent_lab.simulation.environment import open_environment
from tool_agent_lab.simulation import environment as environment_module
from tool_agent_lab.simulation.scenario_generator import generate_scenarios
from tool_agent_lab.simulation.user_simulator import UserSimulator
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect
from tool_agent_lab.storage.task_repository import TaskRepository

SETTINGS = load_settings()
SCENARIOS = generate_scenarios(SETTINGS.business_data_dir)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.scenario_id)
def test_all_generated_scenarios_use_real_mcp_and_owned_receipts(tmp_path, scenario):
    async def run():
        async with open_environment(scenario, root=tmp_path,
                                    mock_responses=scripted_replies(scenario, SETTINGS.business_data_dir)) as environment:
            directory = environment.settings.runtime_dir
            report = await environment.run()
            state = report["state"]
            assert report["task"]["status"] == "completed"
            assert state["result"]["outcome"] == scenario.expected.outcome
            assert sorted(op["action"] for op in report["operations"]) == sorted(scenario.expected.new_actions)
            if scenario.expected.order_id:
                order = next(o for o in report["orders"] if o["order_id"] == scenario.expected.order_id)
                assert order["refunded_amount_minor"] == scenario.expected.refund_total_minor
                assert order["coupon_amount_minor"] == scenario.expected.coupon_total_minor
            else:
                assert report["operations"][0]["result"]["reason"] == "order_unresolved"
            assert bool(state["input_receipts"]) == scenario.expected.must_clarify
            assert any(r["tool"] == "get_operation" for r in state["tool_results"]) == scenario.expected.must_query_operation
            assert all(event["occurred_at"] == scenario.business_time.isoformat() for event in report["events"])
            assert all(event["thread_id"] == state["identity"]["thread_id"] for event in report["events"])
            assert state["identity"]["model_version"] == environment.experiment.model.version
            assert state["identity"]["config_version"] == environment.experiment.agent.version
            assert environment.settings.checkpoint_db_path.is_file()
            assert json.loads((environment.settings.log_dir / "trajectory.json").read_text(encoding="utf-8")) == report
            calls = [c["id"] for m in state["messages"] if m["role"] == "assistant" for c in m.get("tool_calls", [])]
            results = [m["tool_call_id"] for m in state["messages"] if m["role"] == "tool"]
            assert calls == results and len(calls) == len(set(calls))
            with connect(environment.settings.app_db_path) as connection:
                tasks = TaskRepository(connection)
                for receipt in state["approval_receipts"]:
                    request = ApprovalRequest.model_validate(receipt["request"])
                    service = ApprovalService(environment.settings.app_db_path, business_time=scenario.business_time)
                    assert service.record(environment.task.task_id, request, owner_id=scenario.owner_id).model_dump(mode="json") == receipt
                    consumed = tasks.approval_consumed_at(environment.task.task_id, request.request_id, scenario.owner_id)
                    assert (consumed is not None) == (request.decision == "approved")
                assert connection.execute("SELECT count(*) FROM operations WHERE task_id=?", (environment.task.task_id,)).fetchone()[0] == len(report["operations"])
        assert not directory.exists()

    asyncio.run(run())


def test_two_live_trajectories_and_reset_do_not_share_business_or_graph_state(tmp_path):
    scenario = SCENARIOS[0]

    async def run():
        replies = scripted_replies(scenario, SETTINGS.business_data_dir)
        async with open_environment(scenario, root=tmp_path, mock_responses=replies) as first:
            async with open_environment(scenario, root=tmp_path, mock_responses=replies) as second:
                first_report = await first.run()
                assert first.settings.app_db_path != second.settings.app_db_path
                assert first.settings.checkpoint_db_path != second.settings.checkpoint_db_path
                assert first.task.task_id != second.task.task_id
                with connect(second.settings.app_db_path) as connection:
                    assert BusinessRepository(connection).get_order("ORD-1001", scenario.owner_id).refunded_amount_minor == 0
                second_report = await second.run()
                assert first_report["state"]["identity"]["thread_id"] != second_report["state"]["identity"]["thread_id"]
                assert len(first_report["operations"]) == len(second_report["operations"]) == 1
                assert first_report["operations"][0]["operation_key"] != second_report["operations"][0]["operation_key"]
        async with open_environment(scenario, root=tmp_path, mock_responses=replies) as reset:
            with connect(reset.settings.app_db_path) as connection:
                assert BusinessRepository(connection).get_order("ORD-1001", scenario.owner_id).refunded_amount_minor == 0
            assert (await reset.run())["state"]["result"]["outcome"] == "resolved"
        assert not list(tmp_path.glob("trajectory-*"))

    asyncio.run(run())


@pytest.mark.parametrize("waiting_kind", ["input", "approval"])
def test_exhausted_user_script_preserves_waiting_without_authorizing(tmp_path, waiting_kind):
    original = next(s for s in SCENARIOS if s.scenario_id == "clarify-order-before-refund") if waiting_kind == "input" else SCENARIOS[0]
    scenario = original.model_copy(update={"user": UserBehaviour()})

    async def run():
        async with open_environment(scenario, root=tmp_path, mock_responses=scripted_replies(scenario, SETTINGS.business_data_dir)) as environment:
            report = await environment.run()
            assert report["task"]["status"] == f"waiting_{waiting_kind}"
            assert report["stop_reason"] == "user_script_exhausted"
            assert report["operations"] == []
            assert not report["state"]["approval_receipts"]
            assert not report["state"]["input_receipts"]
            with connect(environment.settings.app_db_path) as connection:
                assert BusinessRepository(connection).get_order("ORD-1001", scenario.owner_id).refunded_amount_minor == 0

    asyncio.run(run())


def test_retained_exception_has_diagnostics_and_releases_sqlite_handles(tmp_path):
    scenario = SCENARIOS[0]

    async def run():
        with pytest.raises(ValueError, match="Mock replies exhausted"):
            async with open_environment(scenario, root=tmp_path, keep_artifacts=True) as environment:
                directory = environment.settings.runtime_dir
                await environment.run()
        report = json.loads((directory / "logs/trajectory.json").read_text(encoding="utf-8"))
        assert report["error"]["type"] == "ValueError"
        assert report["operations"] == []
        for path in (directory / "app.sqlite3", directory / "checkpoints.sqlite3"):
            renamed = path.with_suffix(".closed")
            path.rename(renamed)
            renamed.rename(path)
        async with open_environment(scenario, root=tmp_path) as failed:
            failed_dir = failed.settings.runtime_dir
            with pytest.raises(ValueError, match="Mock replies exhausted"):
                await failed.run()
        assert not failed_dir.exists()

    asyncio.run(run())


@pytest.mark.parametrize("field,value", [("fixture_version", "other"), ("business_time", SCENARIOS[0].business_time + timedelta(days=1))])
def test_mismatched_fixture_binding_is_rejected_before_allocating_resources(tmp_path, field, value):
    async def run():
        with pytest.raises(ValueError, match="fixture version or business clock"):
            async with open_environment(SCENARIOS[0].model_copy(update={field: value}), root=tmp_path):
                pytest.fail("Mismatched fixture accepted")
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


def test_experiment_model_identity_is_checked(tmp_path):
    async def run():
        config = ExperimentConfig(model=ModelConfig(provider="mock", name="custom", version="custom-v1"), agent=load_agent_config())
        with pytest.raises(ValueError, match="experiment configuration"):
            async with open_environment(SCENARIOS[0], experiment=config, model=ModelClient(ModelConfig(provider="mock", name="other", version="other")), root=tmp_path):
                pytest.fail("Mismatched client accepted")

    asyncio.run(run())


def test_injected_model_uses_the_shared_generate_contract_without_receiving_labels(tmp_path):
    scenario = next(s for s in SCENARIOS if s.scenario_id == "query-order")

    class RecordingModel(ModelClient):
        def __init__(self):
            super().__init__(ModelConfig(provider="mock", name="injected", version="injected-v1"),
                             mock_responses=scripted_replies(scenario, SETTINGS.business_data_dir))
            self.histories = []

        async def generate(self, messages, *, tools=()):
            self.histories.append(messages)
            assert len(tools) == 7
            return await super().generate(messages, tools=tools)

    async def run():
        model = RecordingModel()
        config = ExperimentConfig(model=model.config, agent=load_agent_config())
        async with open_environment(scenario, experiment=config, model=model, root=tmp_path) as environment:
            report = await environment.run()
            assert report["state"]["identity"]["model_version"] == "injected-v1"
            assert len(model.histories) == 2
            for history in model.histories:
                serialized = json.dumps(history)
                assert "refund_total_minor" not in serialized
                assert "approval_decisions" not in serialized
                assert "family_id" not in serialized

    asyncio.run(run())


def test_scenario_generation_is_deterministic_and_expectations_stay_out_of_model_input():
    assert SCENARIOS == generate_scenarios(SETTINGS.business_data_dir)
    assert {s.task_type for s in SCENARIOS} == {"policy_refund", "refund_and_coupon", "reconcile_operation", "clarify_or_handoff"}
    assert len({s.scenario_id for s in SCENARIOS}) == len(SCENARIOS)
    assert Scenario.model_validate_json(SCENARIOS[0].model_dump_json()) == SCENARIOS[0]
    assert set(SCENARIOS[0].request.model_dump()) == {"user_message", "order_id"}
    with pytest.raises(ValidationError):
        Scenario.model_validate(SCENARIOS[0].model_dump() | {"expected": SCENARIOS[0].expected.model_dump() | {"refund_total_minor": 1.5}})


def test_simulator_polls_reuse_requests_and_new_proposal_versions_consume_new_decisions():
    simulator = UserSimulator(UserBehaviour(input_messages=("ORD-1001",), approval_decisions=("approved", "rejected")))
    waiting = {"kind": "input", "request_id": "current-input"}
    assert simulator.request(waiting) is simulator.request(waiting)
    assert simulator.input_index == 1
    assert simulator.request({"kind": "input", "request_id": "next-input"}) is None
    proposal = {"kind": "approval", "proposal": {"proposal_id": "proposal", "proposal_version": 1}}
    first = simulator.request(proposal)
    assert first is simulator.request(proposal)
    assert first.decision == "approved"
    proposal["proposal"]["proposal_version"] = 2
    second = simulator.request(proposal)
    assert second.decision == "rejected" and second.request_id != first.request_id


@pytest.mark.parametrize("winerror", [32, 5])
def test_cleanup_retries_only_the_reproduced_windows_sharing_error(tmp_path, monkeypatch, winerror):
    remove = environment_module.shutil.rmtree
    attempts = []

    def occupied_once(path):
        attempts.append(path)
        if len(attempts) == 1:
            error = PermissionError("Directory temporarily occupied")
            error.winerror = winerror
            raise error
        remove(path)

    monkeypatch.setattr(environment_module.shutil, "rmtree", occupied_once)

    async def run():
        async with open_environment(SCENARIOS[0], root=tmp_path) as environment:
            return environment.settings.runtime_dir

    if winerror == 32:
        directory = asyncio.run(run())
        assert len(attempts) == 2 and not directory.exists()
    else:
        with pytest.raises(PermissionError, match="temporarily occupied"):
            asyncio.run(run())
        assert len(attempts) == 1


def test_demo_keeps_explicit_mock_when_application_model_environment_is_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("TTAL_MODEL_PROVIDER", "local")
    report = asyncio.run(run_demo(Namespace(scenario="query-order", output_dir=tmp_path, keep_artifacts=False)))
    assert report["model_provider"] == "scripted_mock"
    assert report["results"][0]["status"] == "completed"
    assert list(tmp_path.iterdir()) == []
