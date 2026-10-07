"""Recheck persisted authorization and atomically commit simulated business writes."""

from datetime import datetime
from pathlib import Path

from tool_agent_lab.business.rules import BusinessRules, RuleDecision
from tool_agent_lab.schemas.actions import (
    ActionParameters,
    ActionProposal,
    Approval,
    ApprovalDecision,
    ExecutionContext,
    HandoffAction,
    RefundAction,
)
from tool_agent_lab.schemas.business import Operation, Order
from tool_agent_lab.schemas.tasks import Attempt, AttemptIdentity, Task, TaskStatus
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect, transaction
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

    def prepare(
        self, action: ActionParameters, context: ExecutionContext, *,
        clarification_attempted: bool = False,
    ) -> Operation:
        """Commit a stable pending key before a tool call; never consume approval here."""
        with transaction(self.database) as connection:
            tasks = TaskRepository(connection)
            business = BusinessRepository(connection)
            task, attempt, proposal, approval = self._authorization(tasks, action, context)
            binding = context.write_binding
            existing = business.get_operation(binding.operation_key, context.owner_id)
            if existing is not None:
                self._check_operation_binding(existing, action, context)
                if existing.status != "pending":
                    return existing
            else:
                if business.operation_key_exists(binding.operation_key):
                    raise BusinessError("operation_key_conflict")
                if business.get_operation_for_approval(binding.approval_request_id, context.owner_id) is not None:
                    raise BusinessError("approval_already_bound")

            prepared_at = existing.created_at if existing is not None else context.business_time
            self._check_pending_authorization(tasks, task, attempt, proposal, approval, context, prepared_at)
            self._eligible_business(business, action, context, clarification_attempted)
            if existing is not None:
                return existing
            operation = Operation(
                operation_key=binding.operation_key, owner_id=context.owner_id,
                **self._operation_binding(action, context),
                status="pending", created_at=prepared_at,
            )
            business.add_operation(operation)
            return operation

    def get_operation(self, operation_key: str, context: ExecutionContext) -> Operation | None:
        """Read an owned ledger result without requiring an active task or approval."""
        with connect(self.database) as connection:
            self._owned_attempt(TaskRepository(connection), context)
            return BusinessRepository(connection).get_operation(operation_key, context.owner_id)

    def execute(
        self, action: ActionParameters, context: ExecutionContext, *,
        clarification_attempted: bool = False,
    ) -> Operation:
        """Execute a prepared operation using runtime-supplied context and clarification facts.

        The operation must already be persisted as pending before the tool call.
        Context and clarification_attempted are trusted runtime inputs, never model arguments.
        Rejections leave the pending operation and approval untouched.
        """
        with transaction(self.database) as connection:
            tasks = TaskRepository(connection)
            business = BusinessRepository(connection)
            task, attempt, proposal, approval = self._authorization(tasks, action, context)
            binding = context.write_binding
            operation = business.get_operation(binding.operation_key, context.owner_id)
            if operation is None:
                raise BusinessError("operation_not_prepared")
            self._check_operation_binding(operation, action, context)
            # A committed result remains readable after expiry, cancellation or a new proposal.
            if operation.status == "succeeded":
                return operation
            if operation.status != "pending":
                raise BusinessError("operation_not_pending")

            self._check_pending_authorization(
                tasks, task, attempt, proposal, approval, context, operation.created_at,
            )
            order, decision = self._eligible_business(business, action, context, clarification_attempted)
            now = context.business_time
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

    def _owned_attempt(self, tasks: TaskRepository, context: ExecutionContext) -> tuple[Task, Attempt]:
        task = tasks.get_task(context.task_id, context.owner_id)
        attempt = tasks.get_attempt(context.task_id, context.attempt_id, context.owner_id)
        if task is None or attempt is None:
            raise BusinessError("task_not_found")
        if any(getattr(attempt, name) != getattr(context, name) for name in AttemptIdentity.model_fields):
            raise BusinessError("attempt_identity_mismatch")
        return task, attempt

    def _authorization(
        self, tasks: TaskRepository, action: ActionParameters, context: ExecutionContext,
    ) -> tuple[Task, Attempt, ActionProposal, Approval]:
        binding = context.write_binding
        if binding is None:
            raise BusinessError("confirmation_required")
        task, attempt = self._owned_attempt(tasks, context)
        proposal = tasks.get_proposal(
            context.task_id, binding.proposal_id, context.owner_id, version=binding.proposal_version,
        )
        if proposal is None or proposal.attempt_id != context.attempt_id or proposal.parameters != action:
            raise BusinessError("proposal_mismatch")
        approval = tasks.get_approval(context.task_id, binding.approval_request_id, context.owner_id)
        if approval is None or approval.request.decision != ApprovalDecision.APPROVED:
            raise BusinessError("confirmation_required")
        if approval.proposal != proposal:
            raise BusinessError("confirmation_mismatch")
        return task, attempt, proposal, approval

    def _operation_binding(self, action: ActionParameters, context: ExecutionContext) -> dict:
        return {
            "task_id": context.task_id, "attempt_id": context.attempt_id,
            "order_id": action.order_id, "action": action.action,
            "amount_minor": None if isinstance(action, HandoffAction) else action.amount_minor,
            "currency": "CNY", "approval_request_id": context.write_binding.approval_request_id,
        }

    def _check_operation_binding(
        self, operation: Operation, action: ActionParameters, context: ExecutionContext,
    ) -> None:
        if any(getattr(operation, name) != value for name, value in self._operation_binding(action, context).items()):
            raise BusinessError("operation_binding_mismatch")

    def _check_pending_authorization(
        self, tasks: TaskRepository, task: Task, attempt: Attempt, proposal: ActionProposal,
        approval: Approval, context: ExecutionContext, prepared_at: datetime,
    ) -> None:
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
        if not proposal.created_at <= approval.decided_at <= prepared_at <= now:
            raise BusinessError("authorization_time_mismatch")
        if tasks.approval_consumed_at(context.task_id, context.write_binding.approval_request_id, context.owner_id) is not None:
            raise BusinessError("confirmation_consumed")

    def _eligible_business(
        self, business: BusinessRepository, action: ActionParameters, context: ExecutionContext,
        clarification_attempted: bool,
    ) -> tuple[Order | None, RuleDecision]:
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
            action, order, business_time=context.business_time,
            clarification_attempted=clarification_attempted,
        )
        if not decision.allowed:
            raise BusinessError(decision.code.value)
        return order, decision
