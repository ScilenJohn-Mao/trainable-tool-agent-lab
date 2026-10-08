"""Compare declarative business cases with independently recorded decisions."""

import json
from datetime import datetime

import pytest
from pydantic import TypeAdapter

from tool_agent_lab.business.rules import BusinessRules, RuleDecision
from tool_agent_lab.schemas.actions import ActionParameters
from tool_agent_lab.schemas.business import Order
from tool_agent_lab.settings import PROJECT_ROOT


CASES = [json.loads(line) for line in
         (PROJECT_ROOT / "data/cases/boundary.jsonl").read_text(encoding="utf-8").splitlines()]


def test_case_catalog_is_unique_self_contained_and_covers_the_business_actions():
    assert len(CASES) == 32
    assert len({case["case_id"] for case in CASES}) == len(CASES)
    assert len({case["rationale"] for case in CASES}) == len(CASES)
    assert {case["input"]["action"]["action"] for case in CASES} == {
        "request_refund", "issue_coupon", "create_handoff",
    }
    for case in CASES:
        assert set(case) == {"case_id", "rationale", "input", "expected"}
        assert case["case_id"] and case["rationale"].strip()
        assert set(case["input"]) == {"order", "action", "business_time", "clarification_attempted"}
        assert type(case["input"]["clarification_attempted"]) is bool
        assert set(case["expected"]) == set(RuleDecision.model_fields)
        assert datetime.fromisoformat(case["input"]["business_time"]).utcoffset() is not None


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"])
def test_business_case_matches_its_manual_expectation_without_writes(case):
    rules = BusinessRules.from_file(PROJECT_ROOT / "data/business/v1/spec.json")
    inputs = case["input"]
    action = TypeAdapter(ActionParameters).validate_python(inputs["action"])
    order = Order.model_validate(inputs["order"]) if inputs["order"] is not None else None
    expected = RuleDecision.model_validate(case["expected"])
    snapshot = json.dumps(case, sort_keys=True)
    action_snapshot = action.model_dump_json()
    order_snapshot = order.model_dump_json() if order is not None else None
    actual = rules.evaluate(action, order,
                            business_time=datetime.fromisoformat(inputs["business_time"]),
                            clarification_attempted=inputs["clarification_attempted"])
    assert actual == expected, case["rationale"]
    assert json.dumps(case, sort_keys=True) == snapshot
    assert action.model_dump_json() == action_snapshot
    if order is not None:
        assert order.model_dump_json() == order_snapshot
