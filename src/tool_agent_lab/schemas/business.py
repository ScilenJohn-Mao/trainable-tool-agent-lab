"""Typed order balances and operation ledger records."""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from tool_agent_lab.schemas.common import ContractModel, NonEmptyStr, PositiveInt

NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]


class Order(ContractModel):
    order_id: NonEmptyStr
    owner_id: NonEmptyStr
    product_name: NonEmptyStr
    category: NonEmptyStr
    currency: Literal["CNY"] = "CNY"
    paid_amount_minor: PositiveInt
    promised_delivery_at: AwareDatetime
    delivered_at: AwareDatetime
    damage_verified: bool = Field(strict=True)
    refunded_amount_minor: NonNegativeInt = 0
    coupon_amount_minor: NonNegativeInt = 0

    @model_validator(mode="after")
    def check_refund_balance(self) -> Self:
        if self.refunded_amount_minor > self.paid_amount_minor:
            raise ValueError("refunded amount exceeds paid amount")
        return self


class Operation(ContractModel):
    operation_key: NonEmptyStr
    task_id: NonEmptyStr | None = None
    attempt_id: NonEmptyStr | None = None
    owner_id: NonEmptyStr
    order_id: NonEmptyStr | None = None
    action: Literal["request_refund", "issue_coupon", "create_handoff"]
    amount_minor: PositiveInt | None = None
    currency: Literal["CNY"] = "CNY"
    approval_request_id: NonEmptyStr
    status: Literal["pending", "succeeded", "failed"]
    created_at: AwareDatetime
    committed_at: AwareDatetime | None = None
    result: JsonValue = None

    @model_validator(mode="after")
    def check_record_shape(self) -> Self:
        if (self.task_id is None) != (self.attempt_id is None):
            raise ValueError("task_id and attempt_id must be supplied together")
        if self.action == "create_handoff":
            if self.amount_minor is not None:
                raise ValueError("handoffs do not have a monetary amount")
        elif self.order_id is None or self.amount_minor is None:
            raise ValueError("monetary operations require an order and amount")
        if (self.status == "succeeded") != (self.committed_at is not None):
            raise ValueError("only succeeded operations must have committed_at")
        return self
