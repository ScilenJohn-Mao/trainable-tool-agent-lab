"""Generate repeatable interaction variants from the reviewed business fixtures."""

import json
from pathlib import Path

from scripts.check_business_data import check_business_data
from tool_agent_lab.schemas.scenarios import Scenario, ScenarioExpectation, UserBehaviour
from tool_agent_lab.schemas.tasks import TaskCreate


def generate_scenarios(data_dir: str | Path) -> tuple[Scenario, ...]:
    """Return small runnable examples; dataset splitting and freezing are separate."""
    data_dir = Path(data_dir)
    checked = check_business_data(data_dir)
    rows = json.loads((data_dir / "scenarios.json").read_text(encoding="utf-8"))
    orders = {row["order_id"]: row for row in json.loads((data_dir / "orders.json").read_text(encoding="utf-8"))}
    scenarios = []
    for row in rows:
        expected = ScenarioExpectation(**row["expected"], outcome="resolved" if row["expected"]["new_actions"] else "answered")
        message = row["user_message"]
        if row["known_operation_key"]:
            message += f"\nKnown operation key: {row['known_operation_key']}"
        scenario = Scenario(
            scenario_id=row["scenario_id"], family_id=row["scenario_id"], task_type=row["task_type"],
            fixture_version=checked["data_version"], business_time=checked["business_time"],
            owner_id=row["owner_id"], request=TaskCreate(user_message=message, order_id=row["order_id"]),
            known_operation_key=row["known_operation_key"], expected=expected,
            user=UserBehaviour(
                input_messages=(row["clarification_reply"]["message"],) if row["clarification_reply"] else (),
                approval_decisions=("approved",) * len(expected.new_actions),
            ),
        )
        scenarios.append(scenario)
        if expected.new_actions:
            initial = orders[expected.order_id]
            scenarios.append(Scenario.model_validate(scenario.model_dump() | {
                "scenario_id": scenario.scenario_id + "-rejected",
                "user": scenario.user.model_dump() | {"approval_decisions": ("rejected",)},
                "expected": expected.model_dump() | {
                    "refund_total_minor": initial["refunded_amount_minor"],
                    "coupon_total_minor": initial["coupon_amount_minor"], "new_actions": (),
                    "handoff_reason": None, "outcome": "rejected",
                },
            }))

    clarification = next(s for s in scenarios if s.scenario_id == "clarify-order-before-refund")
    scenarios.append(Scenario.model_validate(clarification.model_dump() | {
        "scenario_id": "clarify-order-insufficient",
        "user": {"input_messages": ("I cannot identify the order. Please arrange manual help.",),
                 "approval_decisions": ("approved",)},
        "expected": {"order_id": None, "refund_total_minor": 0, "coupon_total_minor": 0,
                     "new_actions": ("create_handoff",), "must_clarify": True,
                     "handoff_reason": "order_unresolved", "rule_ids": ("R-HANDOFF-01",), "outcome": "resolved"},
    }))
    refund = scenarios[0]
    initial = orders[refund.expected.order_id]
    scenarios.append(Scenario.model_validate(refund.model_dump() | {
        "scenario_id": "query-order", "request": {
            "user_message": f"Please query order {initial['order_id']} and its paid amount.", "order_id": initial["order_id"],
        }, "user": {},
        "expected": {"order_id": initial["order_id"], "refund_total_minor": initial["refunded_amount_minor"],
                     "coupon_total_minor": initial["coupon_amount_minor"], "outcome": "answered"},
    }))
    return tuple(scenarios)
