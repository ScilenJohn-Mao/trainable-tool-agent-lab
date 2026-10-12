"""Demonstrate isolated simulation with explicitly scripted model replies and real MCP."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from tool_agent_lab.agent.config import load_agent_config
from tool_agent_lab.agent.model_client import AssistantMessage, FunctionCall, ModelConfig, ModelToolCall
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.schemas.scenarios import ExperimentConfig, Scenario
from tool_agent_lab.settings import load_settings
from tool_agent_lab.simulation.environment import open_environment
from tool_agent_lab.simulation.scenario_generator import generate_scenarios


def scripted_replies(scenario: Scenario, data_dir: Path) -> tuple[AssistantMessage, ...]:
    """Demo fixtures only; these replies do not represent autonomous model performance."""
    rules = BusinessRules.from_file(data_dir / "spec.json")
    orders = {row["order_id"]: row for row in json.loads((data_dir / "orders.json").read_text(encoding="utf-8"))}
    replies = []

    def tool(name, arguments):
        replies.append(AssistantMessage(tool_calls=(ModelToolCall(
            id=f"demo-call-{len(replies)}", function=FunctionCall(name=name, arguments=json.dumps(arguments)),
        ),)))

    if scenario.expected.must_clarify:
        replies.append(AssistantMessage(content='{"kind":"ask_user","question":"Which order needs help?"}'))
    order_id = scenario.expected.order_id
    if order_id:
        tool("get_order", {"order_id": order_id})
    if scenario.known_operation_key:
        tool("get_operation", {"operation_key": scenario.known_operation_key})
    if scenario.scenario_id != "query-order" and not scenario.known_operation_key:
        if scenario.task_type == "refund_and_coupon":
            actions = ("request_refund", "issue_coupon")
        elif scenario.expected.handoff_reason or "handoff" in scenario.family_id:
            actions = ("create_handoff",)
        else:
            actions = ("request_refund",)
        for action in actions:
            refs = [rules.reference(action).model_dump(mode="json")]
            if action == "create_handoff":
                tool(action, {"order_id": order_id, "reason": scenario.expected.handoff_reason or "unsupported_category",
                              "summary": "Manual review requested by the user.", "policy_refs": refs})
            else:
                amount = orders[order_id]["paid_amount_minor"] if action == "request_refund" else rules.spec.rules.delay_coupon.amount_minor
                tool(action, {"order_id": order_id, "amount_minor": amount,
                              "reason": "Reviewed delivery evidence", "policy_refs": refs})
            if scenario.user.approval_decisions and scenario.user.approval_decisions[0] == "rejected":
                break
    replies.append(AssistantMessage(content='{"kind":"final","summary":"Use the recorded facts and receipts."}'))
    return tuple(replies)


async def run(args: argparse.Namespace) -> dict:
    settings = load_settings()
    scenarios = generate_scenarios(settings.business_data_dir)
    selected = scenarios if args.scenario == "all" else tuple(s for s in scenarios if s.scenario_id == args.scenario)
    if not selected:
        raise ValueError(f"Unknown scenario {args.scenario}; choose from: {', '.join(s.scenario_id for s in scenarios)}")
    experiment = ExperimentConfig(
        model=ModelConfig(provider="mock", name="simulation-demo", version="simulation-demo-v1"),
        agent=load_agent_config(),
    )
    results = []
    for scenario in selected:
        async with open_environment(scenario, settings=settings, experiment=experiment, root=args.output_dir, keep_artifacts=args.keep_artifacts,
                                    mock_responses=scripted_replies(scenario, settings.business_data_dir)) as environment:
            report = await environment.run()
            expected_order = next((order for order in report["orders"] if order["order_id"] == scenario.expected.order_id), None)
            results.append({
                "scenario_id": scenario.scenario_id, "task_type": scenario.task_type,
                "task_id": environment.task.task_id, "thread_id": report["state"]["identity"]["thread_id"],
                "status": report["task"]["status"], "outcome": report["state"]["result"]["outcome"] if report["state"]["result"] else None,
                "refund_total_minor": expected_order["refunded_amount_minor"] if expected_order else 0,
                "coupon_total_minor": expected_order["coupon_amount_minor"] if expected_order else 0,
                "new_actions": [op["action"] for op in report["operations"]],
                "artifact_dir": str(environment.settings.runtime_dir) if args.keep_artifacts else None,
            })
    return {"model_provider": "scripted_mock", "tools": "real_mcp", "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="refund-damaged", help="Scenario ID or 'all'")
    parser.add_argument("--output-dir", type=Path, help="Parent directory for independent trajectory folders")
    parser.add_argument("--keep-artifacts", action="store_true", help="Retain databases and JSON logs for inspection")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
