"""Business parameters, proposal snapshots and runtime-supplied write bindings.

Parsing these records does not grant authority. The approval/business services
must load persisted records and check ownership, current versions and consumption.
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from tool_agent_lab.schemas.common import ContractModel, NonEmptyStr, PositiveInt
from tool_agent_lab.schemas.tasks import AttemptIdentity


class PolicyReference(ContractModel):
    policy_id: NonEmptyStr
    version: NonEmptyStr
    section: NonEmptyStr
    effective_from: AwareDatetime
    effective_to: AwareDatetime

    @model_validator(mode="after")
    def check_interval(self) -> Self:
        if self.effective_to <= self.effective_from:
            raise ValueError("policy effective_to must be after effective_from")
        return self


PolicyReferences = Annotated[tuple[PolicyReference, ...], Field(min_length=1)]


class MonetaryAction(ContractModel):
    order_id: NonEmptyStr
    amount_minor: PositiveInt = Field(description="Positive integer CNY fen; 100 fen = 1 yuan")
    currency: Literal["CNY"] = "CNY"
    reason: NonEmptyStr
    policy_refs: PolicyReferences


class RefundAction(MonetaryAction):
    action: Literal["request_refund"] = "request_refund"


class CouponAction(MonetaryAction):
    action: Literal["issue_coupon"] = "issue_coupon"


class HandoffAction(ContractModel):
    action: Literal["create_handoff"] = "create_handoff"
    order_id: NonEmptyStr | None = None
    reason: Literal[
        "unsupported_category", "outside_refund_window", "unverified_damage", "order_unresolved"
    ]
    summary: NonEmptyStr
    policy_refs: PolicyReferences

    @model_validator(mode="after")
    def check_order_reference(self) -> Self:
        if (self.order_id is None) != (self.reason == "order_unresolved"):
            raise ValueError("only order_unresolved handoffs must omit order_id")
        return self


ActionParameters = Annotated[
    RefundAction | CouponAction | HandoffAction, Field(discriminator="action")
]


class ActionProposal(ContractModel):
    """Runtime identity wraps business parameters; a version covers the entire snapshot."""

    task_id: NonEmptyStr
    attempt_id: NonEmptyStr
    proposal_id: NonEmptyStr
    proposal_version: PositiveInt
    parameters: ActionParameters
    created_at: AwareDatetime
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_expiration(self) -> Self:
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("proposal expires_at must be after created_at")
        return self


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalRequest(ContractModel):
    """User response to a displayed proposal; never accepts an owner or new parameters."""

    request_id: NonEmptyStr
    proposal_id: NonEmptyStr
    proposal_version: PositiveInt
    decision: ApprovalDecision


class Approval(ContractModel):
    """Stored response plus the exact proposal snapshot and server-resolved owner."""

    request: ApprovalRequest
    proposal: ActionProposal
    owner_id: NonEmptyStr
    decided_at: AwareDatetime

    @model_validator(mode="after")
    def check_proposal_binding(self) -> Self:
        if (self.request.proposal_id, self.request.proposal_version) != (
            self.proposal.proposal_id, self.proposal.proposal_version
        ):
            raise ValueError("approval request does not match the proposal id and version")
        return self


class WriteBinding(ContractModel):
    """References resolved by runtime; operation_key must be persisted before calling."""

    proposal_id: NonEmptyStr
    proposal_version: PositiveInt
    approval_request_id: NonEmptyStr
    operation_key: NonEmptyStr


class ExecutionContext(AttemptIdentity):
    """Trusted runtime envelope, separate from model-visible business parameters."""

    business_time: AwareDatetime
    write_binding: WriteBinding | None = None
