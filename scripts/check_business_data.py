"""Check the small M1 fixture set and preview its hand-written expectations."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from tool_agent_lab.settings import load_settings


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    require(result.utcoffset() is not None, f"Timestamp needs a timezone: {value}")
    return result


def amount(value: Any, label: str) -> int:
    require(type(value) is int and value >= 0, f"{label}: expected non-negative integer fen")
    return value


def index_records(records: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    indexed = {record[key]: record for record in records}
    require(len(indexed) == len(records), f"Duplicate {key}")
    return indexed


def check_business_data(data_dir: Path) -> dict[str, Any]:
    """Validate fixture consistency; this does not execute business rules or tools."""
    documents = {
        name: json.loads((data_dir / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("spec", "orders", "operations", "scenarios")
    }
    spec = documents["spec"]
    now = timestamp(spec["business_time"])
    require(spec["currency"] == "CNY" and spec["amount_unit"] == "fen", "Use CNY integer fen")
    require(spec["minor_units_per_major"] == 100, "100 fen must equal 1 yuan")
    require(timestamp(spec["effective_from"]) <= now < timestamp(spec["effective_to"]),
            "Business time must be inside the fixture policy interval")
    coupon_amount = amount(spec["rules"]["delay_coupon"]["amount_minor"], "coupon rule")
    orders = index_records(documents["orders"], "order_id")
    operations = index_records(documents["operations"], "operation_key")
    scenarios = index_records(documents["scenarios"], "scenario_id")
    refund_totals = dict.fromkeys(orders, 0)

    for order_id, order in orders.items():
        paid = amount(order["paid_amount_minor"], order_id)
        refunded = amount(order["refunded_amount_minor"], order_id)
        amount(order["coupon_amount_minor"], order_id)
        require(paid > 0 and refunded <= paid, f"{order_id}: refund exceeds payment or payment is zero")
        require(order["currency"] == spec["currency"], f"{order_id}: currency mismatch")
        require(timestamp(order["delivered_at"]) <= now, f"{order_id}: delivery is in the future")
        timestamp(order["promised_delivery_at"])

    # This seed set contains completed full refunds only, not an execution ledger implementation.
    for key, operation in operations.items():
        order = orders[operation["order_id"]]
        require(operation["action"] == "request_refund" and operation["status"] == "succeeded",
                f"{key}: seed operation must be a completed refund")
        value = amount(operation["amount_minor"], key)
        require(value == order["paid_amount_minor"], f"{key}: seed refund must equal full payment")
        require(operation["owner_id"] == order["owner_id"] and operation["currency"] == spec["currency"],
                f"{key}: owner or currency mismatch")
        approval = operation["approval"]
        amount(approval["amount_minor"], f"{key} approval")
        require(all(approval[field] == operation[field] for field in
                    ("owner_id", "order_id", "action", "amount_minor")), f"{key}: approval mismatch")
        require(timestamp(approval["approved_at"]) <= timestamp(operation["committed_at"]) <= now,
                f"{key}: invalid approval/commit time")
        require(operation["policy_version"] == spec["policy_version"] and
                operation["rule_id"] == spec["rules"]["refund"]["rule_id"], f"{key}: policy mismatch")
        refund_totals[operation["order_id"]] += value

    for order_id, order in orders.items():
        require(refund_totals[order_id] == order["refunded_amount_minor"],
                f"{order_id}: initial refund ledger mismatch")
        require(order["coupon_amount_minor"] == 0, f"{order_id}: this seed set has no historical coupons")

    rule_ids = {rule["rule_id"] for rule in spec["rules"].values()}
    for scenario_id, scenario in scenarios.items():
        expected = scenario["expected"]
        order = orders[expected["order_id"]]
        require(scenario["owner_id"] == order["owner_id"], f"{scenario_id}: owner mismatch")
        if scenario["order_id"] is None:
            require(expected["must_clarify"] and scenario["clarification_reply"] is not None,
                    f"{scenario_id}: missing order needs clarification")
            selected_order = scenario["clarification_reply"]["order_id"]
        else:
            selected_order = scenario["order_id"]
            require(not expected["must_clarify"], f"{scenario_id}: unexpected clarification label")
        require(selected_order == expected["order_id"], f"{scenario_id}: order reference mismatch")
        actions = expected["new_actions"]
        require(len(actions) == len(set(actions)) and
                set(actions) <= set(spec["write_actions_requiring_approval"]),
                f"{scenario_id}: invalid or duplicate new action")
        if "request_refund" in actions:
            require(order["refunded_amount_minor"] == 0, f"{scenario_id}: would refund twice")
        refund = order["paid_amount_minor"] if "request_refund" in actions else order["refunded_amount_minor"]
        coupon = coupon_amount if "issue_coupon" in actions else order["coupon_amount_minor"]
        require(amount(expected["refund_total_minor"], scenario_id) == refund and
                amount(expected["coupon_total_minor"], scenario_id) == coupon,
                f"{scenario_id}: expected totals disagree with new actions")
        key = scenario["known_operation_key"]
        require(expected["must_query_operation"] == (key is not None),
                f"{scenario_id}: operation lookup label mismatch")
        if key is not None:
            require(operations[key]["order_id"] == expected["order_id"],
                    f"{scenario_id}: operation belongs to another order")
        if "create_handoff" in actions:
            require(expected["handoff_reason"] in spec["rules"]["handoff"]["reasons"],
                    f"{scenario_id}: handoff reason missing")
        require(set(expected["rule_ids"]) <= rule_ids, f"{scenario_id}: unknown rule reference")

    task_types = {scenario["task_type"] for scenario in scenarios.values()}
    require(task_types == set(spec["task_types"]), "Scenarios must cover the declared task types")
    return {
        "status": "fixture_check_passed",
        "data_version": spec["version"],
        "business_time": spec["business_time"],
        "amount_unit": spec["amount_unit"],
        "order_count": len(orders),
        "historical_operation_count": len(operations),
        "scenario_count": len(scenarios),
        "task_types": sorted(task_types),
        "business_execution": "not_run",
        "expectations": [{"scenario_id": key, **value["expected"]} for key, value in scenarios.items()],
    }


def main() -> None:
    report = check_business_data(load_settings().business_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
