"""Evaluate simulated after-sales eligibility without authorizing or executing writes."""

import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import AwareDatetime, model_validator

from tool_agent_lab.schemas.actions import (
    ActionParameters,
    HandoffAction,
    PolicyReference,
    RefundAction,
)
from tool_agent_lab.schemas.business import Order
from tool_agent_lab.schemas.common import ContractModel, NonEmptyStr, PositiveInt


class _RefundRule(ContractModel):
    rule_id: NonEmptyStr
    category: NonEmptyStr
    requires_verified_damage: Literal[True]
    window_days: PositiveInt
    amount: Literal["full_paid_amount"]
    max_per_order: Literal[1]


class _CouponRule(ContractModel):
    rule_id: NonEmptyStr
    category: NonEmptyStr
    minimum_delay_hours: PositiveInt
    amount_minor: PositiveInt
    max_per_order: Literal[1]
    compatible_with_refund: Literal[True]


class _HandoffRule(ContractModel):
    rule_id: NonEmptyStr
    reasons: tuple[NonEmptyStr, ...]


class _Rules(ContractModel):
    refund: _RefundRule
    delay_coupon: _CouponRule
    handoff: _HandoffRule


class _Specification(ContractModel):
    business_time: AwareDatetime
    policy_version: NonEmptyStr
    effective_from: AwareDatetime
    effective_to: AwareDatetime
    currency: Literal["CNY"]
    amount_unit: Literal["fen"]
    minor_units_per_major: Literal[100]
    rules: _Rules

    @model_validator(mode="after")
    def check_interval(self) -> Self:
        if self.effective_to <= self.effective_from:
            raise ValueError("policy effective_to must be after effective_from")
        return self


class RuleCode(StrEnum):
    ELIGIBLE = "eligible"
    POLICY_NOT_ACTIVE = "policy_not_active"
    POLICY_REFERENCE_MISMATCH = "policy_reference_mismatch"
    CLARIFICATION_REQUIRED = "clarification_required"
    ORDER_UNRESOLVED = "order_unresolved"
    ORDER_MISMATCH = "order_mismatch"
    NOT_DELIVERED = "not_delivered"
    UNSUPPORTED_CATEGORY = "unsupported_category"
    UNVERIFIED_DAMAGE = "unverified_damage"
    OUTSIDE_REFUND_WINDOW = "outside_refund_window"
    ALREADY_REFUNDED = "already_refunded"
    ALREADY_COMPENSATED = "already_compensated"
    INSUFFICIENT_DELAY = "insufficient_delay"
    AMOUNT_MISMATCH = "amount_mismatch"
    HANDOFF_NOT_APPLICABLE = "handoff_not_applicable"
    HANDOFF_REASON_MISMATCH = "handoff_reason_mismatch"


class RuleDecision(ContractModel):
    allowed: bool
    code: RuleCode
    rule_id: NonEmptyStr
    policy_version: NonEmptyStr
    expected_amount_minor: PositiveInt | None = None
    handoff_reason: str | None = None


_RULE_NAMES = {
    "request_refund": "refund",
    "issue_coupon": "delay_coupon",
    "create_handoff": "handoff",
}


class BusinessRules:
    def __init__(self, spec: dict) -> None:
        # Only business parameters are consumed; scenario labels are not rule inputs.
        self.spec = _Specification.model_validate({key: spec[key] for key in _Specification.model_fields})

    @classmethod
    def from_file(cls, path: str | Path) -> Self:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def business_time(self) -> datetime:
        return self.spec.business_time

    def reference(self, action: str) -> PolicyReference:
        name = _RULE_NAMES[action]
        rule = getattr(self.spec.rules, name)
        return PolicyReference(
            policy_id=rule.rule_id, version=self.spec.policy_version,
            section=f"spec.json#/rules/{name}", effective_from=self.spec.effective_from,
            effective_to=self.spec.effective_to,
        )

    def _handoff_reasons(self, order: Order | None, now: datetime, clarified: bool) -> set[str]:
        if order is None:
            return {"order_unresolved"} if clarified else set()
        if order.category != self.spec.rules.refund.category:
            return {"unsupported_category"}
        if order.delivered_at > now or order.refunded_amount_minor > 0:
            return set()
        reasons = set()
        if not order.damage_verified:
            reasons.add("unverified_damage")
        if now - order.delivered_at.astimezone(UTC) > timedelta(days=self.spec.rules.refund.window_days):
            reasons.add("outside_refund_window")
        return reasons

    def _refund_code(self, order: Order, now: datetime) -> RuleCode:
        if order.refunded_amount_minor > 0:
            return RuleCode.ALREADY_REFUNDED
        if order.category != self.spec.rules.refund.category:
            return RuleCode.UNSUPPORTED_CATEGORY
        if order.delivered_at > now:
            return RuleCode.NOT_DELIVERED
        if not order.damage_verified:
            return RuleCode.UNVERIFIED_DAMAGE
        if now - order.delivered_at.astimezone(UTC) > timedelta(days=self.spec.rules.refund.window_days):
            return RuleCode.OUTSIDE_REFUND_WINDOW
        return RuleCode.ELIGIBLE

    def _coupon_code(self, order: Order, now: datetime) -> RuleCode:
        if order.category != self.spec.rules.delay_coupon.category:
            return RuleCode.UNSUPPORTED_CATEGORY
        if order.delivered_at > now:
            return RuleCode.NOT_DELIVERED
        if order.coupon_amount_minor > 0:
            return RuleCode.ALREADY_COMPENSATED
        delay = order.delivered_at.astimezone(UTC) - order.promised_delivery_at.astimezone(UTC)
        if delay < timedelta(hours=self.spec.rules.delay_coupon.minimum_delay_hours):
            return RuleCode.INSUFFICIENT_DELAY
        return RuleCode.ELIGIBLE

    def evaluate(
        self, action: ActionParameters, order: Order | None, *,
        business_time: datetime | None = None, clarification_attempted: bool = False,
    ) -> RuleDecision:
        now = self.business_time if business_time is None else business_time
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("business_time must include a timezone")
        now = now.astimezone(UTC)
        reference = self.reference(action.action)

        def decision(
            code: RuleCode, *, amount: int | None = None, handoff: str | None = None
        ) -> RuleDecision:
            return RuleDecision(
                allowed=code == RuleCode.ELIGIBLE, code=code, rule_id=reference.policy_id,
                policy_version=reference.version, expected_amount_minor=amount,
                handoff_reason=handoff,
            )

        if not self.spec.effective_from <= now < self.spec.effective_to:
            return decision(RuleCode.POLICY_NOT_ACTIVE)
        trusted_refs = tuple(self.reference(name) for name in _RULE_NAMES)
        if reference not in action.policy_refs or any(ref not in trusted_refs for ref in action.policy_refs):
            return decision(RuleCode.POLICY_REFERENCE_MISMATCH)
        if order is not None and action.order_id != order.order_id:
            return decision(RuleCode.ORDER_MISMATCH)
        if order is None and not clarification_attempted:
            return decision(RuleCode.CLARIFICATION_REQUIRED)

        handoff_reasons = self._handoff_reasons(order, now, clarification_attempted)
        if isinstance(action, HandoffAction):
            if action.reason not in self.spec.rules.handoff.reasons:
                return decision(RuleCode.HANDOFF_REASON_MISMATCH)
            if action.reason in handoff_reasons:
                return decision(RuleCode.ELIGIBLE, handoff=action.reason)
            code = RuleCode.HANDOFF_REASON_MISMATCH if handoff_reasons else RuleCode.HANDOFF_NOT_APPLICABLE
            return decision(code)
        if order is None:
            return decision(RuleCode.ORDER_UNRESOLVED, handoff="order_unresolved")

        if isinstance(action, RefundAction):
            code = self._refund_code(order, now)
            expected_amount = order.paid_amount_minor
        else:
            code = self._coupon_code(order, now)
            expected_amount = self.spec.rules.delay_coupon.amount_minor
        if code != RuleCode.ELIGIBLE:
            handoff = code.value if code.value in handoff_reasons else None
            return decision(code, amount=expected_amount, handoff=handoff)
        if action.amount_minor != expected_amount:
            return decision(RuleCode.AMOUNT_MISMATCH, amount=expected_amount)
        return decision(RuleCode.ELIGIBLE, amount=expected_amount)
