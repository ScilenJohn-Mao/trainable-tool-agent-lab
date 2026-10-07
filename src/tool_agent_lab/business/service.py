"""Recheck persisted authorization and atomically commit simulated business writes."""

from pathlib import Path

from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.schemas.actions import (
    ActionParameters,
    ApprovalDecision,
    ExecutionContext,
    HandoffAction,
    RefundAction,
)
from tool_agent_lab.schemas.business import Operation
from tool_agent_lab.schemas.tasks import AttemptIdentity, TaskStatus
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import transaction
from tool_agent_lab.storage.task_repository import TaskRepository


class BusinessError(ValueError):
    """A refused write; code is suitable for a structured tool error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class BusinessService:
    def __init__(self, database: str | Path, rules: BusinessRules) -> None:
        self.database = Path(database)
        self.rules = rules

    def execute(
        self, action: ActionParameters, context: ExecutionContext, *,
        clarification_attempted: bool = False,
    ) -> Operation:
        """Execute a prepared operation using runtime-supplied context and clarification facts.

        The operation must already be persisted as pending before the tool call.
        Context and clarification_attempted are trusted runtime inputs, never model arguments.
        Rejections leave the pending operation and approval untouched.
        """
        binding = context.write_binding
        if binding is None:
            raise BusinessError("confirmation_required")

        with transaction(self.database) as connection:
            tasks = TaskRepository(connection)
            business = BusinessRepository(connection)
            task = tasks.get_task(context.task_id, context.owner_id)
            attempt = tasks.get_attempt(context.task_id, context.attempt_id, context.owner_id)
            if task is None or attempt is None:
                raise BusinessError("task_not_found")
            if any(getattr(attempt, name) != getattr(context, name) for name in AttemptIdentity.model_fields):
                raise BusinessError("attempt_identity_mismatch")

            proposal = tasks.get_proposal(
                context.task_id, binding.proposal_id, context.owner_id,
                version=binding.proposal_version,
            )
            if proposal is None or proposal.attempt_id != context.attempt_id or proposal.parameters != action:
                raise BusinessError("proposal_mismatch")
            approval = tasks.get_approval(context.task_id, binding.approval_request_id, context.owner_id)
            if approval is None or approval.request.decision != ApprovalDecision.APPROVED:
                raise BusinessError("confirmation_required")
            if approval.proposal != proposal:
                raise BusinessError("confirmation_mismatch")

            operation = business.get_operation(binding.operation_key, context.owner_id)
            if operation is None:
                raise BusinessError("operation_not_prepared")
            expected = {
                "task_id": context.task_id, "attempt_id": context.attempt_id,
                "order_id": action.order_id, "action": action.action,
                "amount_minor": None if isinstance(action, HandoffAction) else action.amount_minor,
                "currency": "CNY", "approval_request_id": binding.approval_request_id,
            }
            if any(getattr(operation, name) != value for name, value in expected.items()):
                raise BusinessError("operation_binding_mismatch")
            # A committed result remains readable after expiry, cancellation or a new proposal.
            if operation.status == "succeeded":
                return operation
            if operation.status != "pending":
                raise BusinessError("operation_not_pending")

            allowed_states = {TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL}
            if task.status not in allowed_states or attempt.status not in allowed_states:
                raise BusinessError("task_not_executable")
            if task.current_attempt_id != context.attempt_id:
                raise BusinessError("attempt_not_current")
            if tasks.get_current_proposal(context.task_id, context.attempt_id, context.owner_id) != proposal:
                raise BusinessError("proposal_not_current")
            now = context.business_time
            if proposal.expires_at is not None and now >= proposal.expires_at:
                raise BusinessError("proposal_expired")
            if not proposal.created_at <= approval.decided_at <= operation.created_at <= now:
                raise BusinessError("authorization_time_mismatch")
            if tasks.approval_consumed_at(context.task_id, binding.approval_request_id, context.owner_id) is not None:
                raise BusinessError("confirmation_consumed")

            order = business.get_order(action.order_id, context.owner_id) if action.order_id else None
            if action.order_id is not None and order is None:
                raise BusinessError("order_not_found")
            if isinstance(action, HandoffAction):
                previous = business.get_successful_handoff(
                    context.task_id, context.owner_id, action.order_id, action.reason,
                )
            else:
                previous = business.get_successful_operation(action.order_id, action.action, context.owner_id)
            if previous is not None:
                raise BusinessError("business_action_already_committed")
            decision = self.rules.evaluate(
                action, order, business_time=now, clarification_attempted=clarification_attempted,
            )
            if not decision.allowed:
                raise BusinessError(decision.code.value)

            if not tasks.consume_approval(context.task_id, binding.approval_request_id, context.owner_id, now):
                raise BusinessError("confirmation_consumed")
            if not isinstance(action, HandoffAction):
                refunded = action.amount_minor if isinstance(action, RefundAction) else order.refunded_amount_minor
                coupon = order.coupon_amount_minor if isinstance(action, RefundAction) else action.amount_minor
                if not business.set_balances(
                    order.order_id, context.owner_id,
                    refunded_amount_minor=refunded, coupon_amount_minor=coupon,
                ):
                    raise BusinessError("order_not_found")
            result = {
                "operation_key": operation.operation_key, "action": action.action,
                "order_id": action.order_id, "amount_minor": operation.amount_minor,
                "currency": operation.currency, "rule_id": decision.rule_id,
                "policy_version": decision.policy_version, "reason": action.reason,
            }
            if isinstance(action, HandoffAction):
                result["summary"] = action.summary
            if not business.finish_operation(
                operation.operation_key, context.owner_id, status="succeeded",
                committed_at=now, result=result,
            ):
                raise BusinessError("operation_not_pending")
            return business.get_operation(operation.operation_key, context.owner_id)
