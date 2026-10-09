"""Structured conclusions built from owned ledger and retrieved policy evidence."""

from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue

from tool_agent_lab.schemas.actions import PolicyReference
from tool_agent_lab.schemas.business import Operation
from tool_agent_lab.schemas.common import ContractModel, NonEmptyStr
from tool_agent_lab.schemas.tasks import AttemptIdentity


class QuestionDraft(ContractModel):
    kind: Literal["ask_user"]
    question: NonEmptyStr = Field(max_length=4000)


class ConclusionDraft(ContractModel):
    kind: Literal["final"]
    summary: NonEmptyStr = Field(max_length=4000)


class AgentResult(ContractModel):
    task_id: NonEmptyStr
    attempt_id: NonEmptyStr
    model_version: NonEmptyStr
    outcome: Literal["resolved", "answered", "rejected", "failed"]
    summary: NonEmptyStr
    operations: tuple[Operation, ...] = ()
    facts: dict[str, JsonValue] = Field(default_factory=dict)
    policy_citations: tuple[PolicyReference, ...] = ()
    rule_refs: tuple[PolicyReference, ...] = ()
    failure_reason: str | None = None


def build_result(state: dict, database: str | Path) -> AgentResult:
    from tool_agent_lab.runtime.task_service import TaskError
    from tool_agent_lab.storage.business_repository import BusinessRepository
    from tool_agent_lab.storage.database import connect
    from tool_agent_lab.storage.task_repository import TaskRepository

    identity = AttemptIdentity.model_validate(state["identity"])
    operations, rule_refs = [], []
    facts = dict(state["facts"])
    with connect(database) as connection:
        business, tasks = BusinessRepository(connection), TaskRepository(connection)
        order_ids = set(facts.get("orders", {}))
        for claimed in state["operations"]:
            operation = business.get_operation(claimed["operation_key"], identity.owner_id)
            if operation is None or operation.status != "succeeded" or (
                operation.task_id, operation.attempt_id
            ) != (identity.task_id, identity.attempt_id):
                raise TaskError("result_operation_not_committed")
            approval = tasks.get_approval(identity.task_id, operation.approval_request_id, identity.owner_id)
            if approval is None or tasks.approval_consumed_at(identity.task_id, operation.approval_request_id, identity.owner_id) is None:
                raise TaskError("result_confirmation_not_consumed")
            operations.append(operation)
            rule_refs.extend(approval.proposal.parameters.policy_refs)
            if operation.order_id:
                order_ids.add(operation.order_id)
        facts["orders"] = {
            order_id: order.model_dump(mode="json") for order_id in sorted(order_ids)
            if (order := business.get_order(order_id, identity.owner_id)) is not None
        }
    failure = state["result"].get("reason") if state["status"] == "failed" else None
    if failure:
        outcome, summary = "failed", f"Agent stopped: {failure}."
    elif operations:
        outcome = "resolved"
        summary = "; ".join(
            f"{op.action}: CNY {op.amount_minor} fen, committed ({op.operation_key})"
            if op.amount_minor is not None else f"{op.action}: committed ({op.operation_key})"
            for op in operations
        )
    elif any(r["request"]["decision"] == "rejected" for r in state["approval_receipts"]):
        outcome, summary = "rejected", "Proposal rejected. No business operation was committed."
    else:
        outcome, summary = "answered", "No new business operation was committed. Recorded facts and policies are available."
    return AgentResult(
        task_id=identity.task_id, attempt_id=identity.attempt_id, model_version=identity.model_version,
        outcome=outcome, summary=summary, operations=tuple(operations), facts=facts,
        policy_citations=tuple(PolicyReference.model_validate(ref) for ref in state["citations"]),
        rule_refs=tuple({ref.model_dump_json(): ref for ref in rule_refs}.values()), failure_reason=failure,
    )
