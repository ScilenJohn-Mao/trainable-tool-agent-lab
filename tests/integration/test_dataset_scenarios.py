import asyncio
import json

import pytest

from tool_agent_lab.agent.config import load_agent_config
from tool_agent_lab.agent.model_client import AssistantMessage, FunctionCall, ModelConfig, ModelToolCall
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.evaluation.datasets import build_dataset, intended_action, load_dataset
from tool_agent_lab.schemas.scenarios import ExperimentConfig
from tool_agent_lab.simulation.environment import open_environment
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect


@pytest.fixture(scope="module")
def full_dataset(tmp_path_factory):
    output = tmp_path_factory.mktemp("dataset") / "frozen"
    build_dataset(output)
    return load_dataset(output, "train") + load_dataset(output, "test")


def scripted_inputs(scenario, rules):
    replies = []

    def tool(name, arguments):
        replies.append(AssistantMessage(tool_calls=(ModelToolCall(id="call-" + str(len(replies)),
            function=FunctionCall(name=name, arguments=json.dumps(arguments))),)))

    if scenario.user.input_messages:
        replies.append(AssistantMessage(content='{"kind":"ask_user","question":"Which order?"}'))
    if scenario.initial_state.orders:
        tool("get_order", {"order_id": scenario.initial_state.orders[0].order_id})
    if scenario.known_operation_key:
        tool("get_operation", {"operation_key": scenario.known_operation_key})
    for index, name in enumerate(scenario.requested_actions):
        action = intended_action(name, scenario, rules).model_dump(mode="json")
        action.pop("action")
        tool(name, action)
        if scenario.user.approval_decisions[index] == "rejected":
            break
    replies.append(AssistantMessage(content='{"kind":"final","summary":"Use the recorded facts and receipts."}'))
    return tuple(replies)


@pytest.mark.parametrize("mode,version", [("refund", "business-v1"), ("refund_rejected", "business-v1"),
    ("clarify_refund", "business-v1"), ("unresolved", "business-v1"), ("digital_late", "business-v1"),
    ("refund_boundary", "business-v1"), ("combined_partial", "business-v1"), ("lookup_coupon", "business-v1"),
    ("combined", "business-v2"), ("lookup", "business-v2")])
def test_frozen_instances_run_on_shared_graph_real_mcp_and_versioned_business_state(tmp_path, full_dataset, mode, version):
    scenario = next(s for s in full_dataset if s.mode == mode and s.fixture_version == version)

    async def run():
        rules = BusinessRules.from_file(scenario.initial_state.data_dir / "spec.json")
        config = ExperimentConfig(model=ModelConfig(provider="mock", name="dataset-check", version="dataset-check-v1"),
                                  agent=load_agent_config())
        async with open_environment(scenario, experiment=config, root=tmp_path,
                                    mock_responses=scripted_inputs(scenario, rules)) as environment:
            directory = environment.settings.runtime_dir
            with connect(environment.settings.app_db_path) as connection:
                business = BusinessRepository(connection)
                for order in scenario.initial_state.orders:
                    assert business.get_order(order.order_id, scenario.owner_id) == order
                    assert business.get_order(order.order_id, "other-owner") is None
                if scenario.known_operation_key:
                    receipt = business.get_operation(scenario.known_operation_key, scenario.owner_id)
                    assert receipt.status == "succeeded" and receipt.approval_request_id == scenario.initial_state.historical_refunds[0].approval_request_id
            report = await environment.run()
            assert report["task"]["status"] == "completed"
            assert report["state"]["result"]["outcome"] == scenario.expected.outcome
            assert sorted(op["action"] for op in report["operations"]) == sorted(scenario.expected.new_actions)
            if report["orders"]:
                order = report["orders"][0]
                assert (order["refunded_amount_minor"], order["coupon_amount_minor"]) == (scenario.expected.refund_total_minor, scenario.expected.coupon_total_minor)
            assert bool(report["state"]["input_receipts"]) == scenario.expected.must_clarify
            assert all(e["occurred_at"] == scenario.business_time.isoformat() for e in report["events"])
            assert all(op["task_id"] == environment.task.task_id for op in report["operations"])
            with connect(environment.settings.app_db_path) as connection:
                assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not directory.exists()

    asyncio.run(run())
