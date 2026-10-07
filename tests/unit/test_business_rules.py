"""Verify eligibility against independent amounts and exact business-time boundaries."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from tool_agent_lab.business.rules import BusinessRules, RuleCode, RuleDecision
from tool_agent_lab.schemas.actions import CouponAction, HandoffAction, RefundAction
from tool_agent_lab.schemas.business import Order
from tool_agent_lab.settings import PROJECT_ROOT

DATA_DIR = PROJECT_ROOT / "data/business/v1"


@pytest.fixture
def rules() -> BusinessRules:
    return BusinessRules.from_file(DATA_DIR / "spec.json")


@pytest.fixture
def orders() -> dict[str, Order]:
    return {order["order_id"]: Order.model_validate(order)
        for order in json.loads((DATA_DIR / "orders.json").read_text(encoding="utf-8"))}


def refund(rules: BusinessRules, order: Order, amount: int | None = None) -> RefundAction:
    return RefundAction(order_id=order.order_id,
        amount_minor=order.paid_amount_minor if amount is None else amount,
        reason="已核实损坏", policy_refs=(rules.reference("request_refund"),))


def coupon(rules: BusinessRules, order: Order, amount: int = 500) -> CouponAction:
    return CouponAction(order_id=order.order_id, amount_minor=amount,
        reason="延迟送达", policy_refs=(rules.reference("issue_coupon"),))


def handoff(rules: BusinessRules, order: Order | None, reason: str) -> HandoffAction:
    return HandoffAction(order_id=order.order_id if order is not None else None,
        reason=reason, summary="需要人工核对", policy_refs=(rules.reference("create_handoff"),))


def changed(order: Order, **fields: object) -> Order:
    return Order.model_validate(order.model_dump() | fields)


def test_fixture_refund_coupon_reconciliation_and_digital_handoff(rules: BusinessRules, orders: dict) -> None:
    first = rules.evaluate(refund(rules, orders["ORD-1001"]), orders["ORD-1001"])
    assert first.allowed and first.expected_amount_minor == 12900
    assert rules.evaluate(coupon(rules, orders["ORD-1001"]), orders["ORD-1001"]).code == RuleCode.INSUFFICIENT_DELAY
    assert (first.rule_id, first.policy_version) == ("R-REFUND-01", "mock-policy-v1")
    second = orders["ORD-1002"]
    assert rules.evaluate(refund(rules, second), second).expected_amount_minor == 25900
    assert rules.evaluate(refund(rules, second), second).allowed
    assert rules.evaluate(coupon(rules, second), second).allowed
    already_refunded = orders["ORD-1003"]
    assert rules.evaluate(refund(rules, already_refunded), already_refunded).code == RuleCode.ALREADY_REFUNDED
    digital = orders["ORD-1004"]
    assert rules.evaluate(refund(rules, digital), digital).handoff_reason == "unsupported_category"
    assert rules.evaluate(coupon(rules, digital), digital).code == RuleCode.UNSUPPORTED_CATEGORY
    assert rules.evaluate(handoff(rules, digital, "unsupported_category"), digital).allowed
    assert RuleDecision.model_validate_json(first.model_dump_json()) == first


@pytest.mark.parametrize("extra,expected", [
    (timedelta(microseconds=-1), RuleCode.ELIGIBLE),
    (timedelta(0), RuleCode.ELIGIBLE),
    (timedelta(microseconds=1), RuleCode.OUTSIDE_REFUND_WINDOW),
])
def test_refund_seven_day_boundary(rules: BusinessRules, orders: dict, extra: timedelta, expected: RuleCode) -> None:
    order = changed(orders["ORD-1001"], delivered_at=rules.business_time - timedelta(days=7) - extra)
    decision = rules.evaluate(refund(rules, order), order)
    assert decision.code == expected
    assert decision.allowed == (expected == RuleCode.ELIGIBLE)
    if not decision.allowed:
        assert decision.handoff_reason == "outside_refund_window"


@pytest.mark.parametrize("delay,expected", [
    (timedelta(hours=24, microseconds=-1), RuleCode.INSUFFICIENT_DELAY),
    (timedelta(hours=24), RuleCode.ELIGIBLE),
    (timedelta(hours=24, microseconds=1), RuleCode.ELIGIBLE),
])
def test_coupon_twenty_four_hour_boundary(rules: BusinessRules, orders: dict, delay: timedelta, expected: RuleCode) -> None:
    base = orders["ORD-1002"]
    order = changed(base, promised_delivery_at=base.delivered_at - delay)
    assert rules.evaluate(coupon(rules, order), order).code == expected


@pytest.mark.parametrize("amount", [1, 12899, 12901])
def test_refund_amount_must_equal_full_payment(rules: BusinessRules, orders: dict, amount: int) -> None:
    order = orders["ORD-1001"]
    decision = rules.evaluate(refund(rules, order, amount), order)
    assert decision.code == RuleCode.AMOUNT_MISMATCH and not decision.allowed
    assert decision.expected_amount_minor == 12900


@pytest.mark.parametrize("amount", [1, 499, 501])
def test_coupon_amount_must_equal_specification(rules: BusinessRules, orders: dict, amount: int) -> None:
    order = orders["ORD-1002"]
    decision = rules.evaluate(coupon(rules, order, amount), order)
    assert decision.code == RuleCode.AMOUNT_MISMATCH and decision.expected_amount_minor == 500


@pytest.mark.parametrize("amount", [True, 12.5, "500", 0, -1])
def test_reuse_action_contract_for_invalid_amounts(rules: BusinessRules, orders: dict, amount: object) -> None:
    with pytest.raises(ValidationError, match="amount_minor"):
        coupon(rules, orders["ORD-1002"], amount)


def test_unverified_damage_future_delivery_and_prior_balances(rules: BusinessRules, orders: dict) -> None:
    unverified = changed(orders["ORD-1001"], damage_verified=False)
    decision = rules.evaluate(refund(rules, unverified), unverified)
    assert decision.code == RuleCode.UNVERIFIED_DAMAGE and decision.handoff_reason == "unverified_damage"
    future = changed(orders["ORD-1002"], delivered_at=rules.business_time + timedelta(microseconds=1))
    assert rules.evaluate(refund(rules, future), future).code == RuleCode.NOT_DELIVERED
    assert rules.evaluate(coupon(rules, future), future).code == RuleCode.NOT_DELIVERED
    partial = changed(orders["ORD-1001"], refunded_amount_minor=1)
    assert rules.evaluate(refund(rules, partial), partial).code == RuleCode.ALREADY_REFUNDED
    compensated = changed(orders["ORD-1002"], coupon_amount_minor=500)
    assert rules.evaluate(coupon(rules, compensated), compensated).code == RuleCode.ALREADY_COMPENSATED
    assert rules.evaluate(refund(rules, compensated), compensated).allowed


def test_coupon_does_not_inherit_refund_window_damage_or_refund_state(rules: BusinessRules, orders: dict) -> None:
    base = orders["ORD-1002"]
    delivered = rules.business_time - timedelta(days=8)
    order = changed(base, delivered_at=delivered, promised_delivery_at=delivered - timedelta(hours=24),
        damage_verified=False, refunded_amount_minor=base.paid_amount_minor)
    assert rules.evaluate(coupon(rules, order), order).allowed
    assert rules.evaluate(refund(rules, order), order).code == RuleCode.ALREADY_REFUNDED


@pytest.mark.parametrize("kind", ["before", "start", "end-minus", "end"])
def test_policy_interval_is_start_inclusive_end_exclusive(rules: BusinessRules, orders: dict, kind: str) -> None:
    spec = json.loads((DATA_DIR / "spec.json").read_text(encoding="utf-8"))
    start, end = [datetime.fromisoformat(spec[key]) for key in ("effective_from", "effective_to")]
    now = {"before": start - timedelta(microseconds=1), "start": start,
        "end-minus": end - timedelta(microseconds=1), "end": end}[kind]
    order = changed(orders["ORD-1001"], delivered_at=now - timedelta(days=1))
    decision = rules.evaluate(refund(rules, order), order, business_time=now)
    assert decision.code == (RuleCode.ELIGIBLE if kind in ("start", "end-minus") else RuleCode.POLICY_NOT_ACTIVE)


@pytest.mark.parametrize("field,value", [
    ("policy_id", "untrusted-rule"), ("version", "old-policy"),
    ("section", "wrong-section"), ("effective_from", "2026-09-02T00:00:00+08:00"),
])
def test_policy_reference_must_match_trusted_spec(rules: BusinessRules, orders: dict, field: str, value: str) -> None:
    action = refund(rules, orders["ORD-1001"])
    reference = action.policy_refs[0].model_dump() | {field: value}
    altered = RefundAction.model_validate(action.model_dump() | {"policy_refs": [reference]})
    assert rules.evaluate(altered, orders["ORD-1001"]).code == RuleCode.POLICY_REFERENCE_MISMATCH


def test_wrong_action_reference_and_untrusted_extra_reference_are_rejected(rules: BusinessRules, orders: dict) -> None:
    order = orders["ORD-1001"]
    action = refund(rules, order)
    wrong = RefundAction.model_validate(action.model_dump() | {"policy_refs": [rules.reference("issue_coupon")]})
    assert rules.evaluate(wrong, order).code == RuleCode.POLICY_REFERENCE_MISMATCH
    extra = action.policy_refs[0].model_dump() | {"version": "wrong-version"}
    mixed = RefundAction.model_validate(action.model_dump() | {"policy_refs": [action.policy_refs[0], extra]})
    assert rules.evaluate(mixed, order).code == RuleCode.POLICY_REFERENCE_MISMATCH


def test_missing_order_clarifies_before_unresolved_handoff(rules: BusinessRules, orders: dict) -> None:
    action = refund(rules, orders["ORD-1001"])
    assert rules.evaluate(action, None).code == RuleCode.CLARIFICATION_REQUIRED
    unresolved = handoff(rules, None, "order_unresolved")
    assert rules.evaluate(unresolved, None).code == RuleCode.CLARIFICATION_REQUIRED
    assert rules.evaluate(unresolved, None, clarification_attempted=True).allowed
    assert rules.evaluate(action, None, clarification_attempted=True).handoff_reason == "order_unresolved"
    assert rules.evaluate(action, orders["ORD-1001"]).allowed
    assert rules.evaluate(unresolved, orders["ORD-1001"], clarification_attempted=True).code == RuleCode.ORDER_MISMATCH
    assert rules.evaluate(action, orders["ORD-1002"]).code == RuleCode.ORDER_MISMATCH


def test_handoff_reason_must_be_supported_by_order_facts(rules: BusinessRules, orders: dict) -> None:
    valid = orders["ORD-1001"]
    assert rules.evaluate(handoff(rules, valid, "unverified_damage"), valid).code == RuleCode.HANDOFF_NOT_APPLICABLE
    expired = changed(valid, delivered_at=rules.business_time - timedelta(days=7, microseconds=1))
    assert rules.evaluate(handoff(rules, expired, "outside_refund_window"), expired).allowed
    assert rules.evaluate(handoff(rules, expired, "unverified_damage"), expired).code == RuleCode.HANDOFF_REASON_MISMATCH
    unverified = changed(valid, damage_verified=False)
    assert rules.evaluate(handoff(rules, unverified, "unverified_damage"), unverified).allowed
    both = changed(expired, damage_verified=False)
    for reason in ("unverified_damage", "outside_refund_window"):
        assert rules.evaluate(handoff(rules, both, reason), both).allowed
    refunded = orders["ORD-1003"]
    assert rules.evaluate(handoff(rules, refunded, "outside_refund_window"), refunded).code == RuleCode.HANDOFF_NOT_APPLICABLE


def test_rules_use_loaded_parameters_and_reject_unsupported_rule_shapes(orders: dict) -> None:
    spec = json.loads((DATA_DIR / "spec.json").read_text(encoding="utf-8"))
    spec["rules"]["delay_coupon"]["amount_minor"] = 800
    spec["rules"]["delay_coupon"]["minimum_delay_hours"] = 30
    spec["rules"]["refund"]["window_days"] = 1
    rules = BusinessRules(spec)
    assert rules.evaluate(refund(rules, orders["ORD-1001"]), orders["ORD-1001"]).code == RuleCode.OUTSIDE_REFUND_WINDOW
    second = orders["ORD-1002"]
    assert rules.evaluate(coupon(rules, second, 800), second).code == RuleCode.INSUFFICIENT_DELAY
    delayed = changed(second, promised_delivery_at=second.delivered_at - timedelta(hours=30))
    assert rules.evaluate(coupon(rules, delayed, 800), delayed).allowed
    assert rules.evaluate(coupon(rules, delayed), delayed).code == RuleCode.AMOUNT_MISMATCH
    spec["rules"]["refund"]["max_per_order"] = 2
    with pytest.raises(ValidationError, match="max_per_order"):
        BusinessRules(spec)


def test_fixed_business_time_timezone_equivalence_and_no_mutation(rules: BusinessRules, orders: dict) -> None:
    order = orders["ORD-1002"]
    snapshot = order.model_dump_json()
    original = rules.evaluate(coupon(rules, order), order)
    assert rules.business_time.isoformat() == "2026-09-17T12:00:00+08:00"
    converted = changed(order, delivered_at=order.delivered_at.astimezone(UTC),
        promised_delivery_at=order.promised_delivery_at.astimezone(UTC))
    assert rules.evaluate(coupon(rules, converted), converted, business_time=rules.business_time.astimezone(UTC)) == original
    with pytest.raises(ValueError, match="timezone"):
        rules.evaluate(coupon(rules, order), order, business_time=rules.business_time.replace(tzinfo=None))
    assert order.model_dump_json() == snapshot
