"""Verify authorization and atomic business writes with persisted confirmation fixtures."""

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from scripts.check_contracts import check_contracts
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.business.service import BusinessError, BusinessService
from tool_agent_lab.schemas.actions import (
    ActionProposal, Approval, ApprovalRequest, CouponAction, ExecutionContext, HandoffAction,
)
from tool_agent_lab.schemas.business import Operation, Order
from tool_agent_lab.schemas.tasks import Attempt, Task, TaskStatus
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect, initialize_database, transaction
from tool_agent_lab.storage.task_repository import TaskRepository


def prepare(path: Path, context: ExecutionContext, proposal: ActionProposal, *, decision: str = "approved") -> None:
    """Persist synthetic confirmation and pending records to exercise the business layer."""
    binding = context.write_binding
    with transaction(path) as connection:
        tasks = TaskRepository(connection)
        tasks.add_proposal(proposal, context.owner_id)
        tasks.add_approval(Approval(
            request=ApprovalRequest(
                request_id=binding.approval_request_id, proposal_id=proposal.proposal_id,
                proposal_version=proposal.proposal_version, decision=decision,
            ),
            proposal=proposal, owner_id=context.owner_id, decided_at=context.business_time,
        ))
        action = proposal.parameters
        BusinessRepository(connection).add_operation(Operation(
            operation_key=binding.operation_key, task_id=context.task_id,
            attempt_id=context.attempt_id, owner_id=context.owner_id, order_id=action.order_id,
            action=action.action, amount_minor=None if isinstance(action, HandoffAction) else action.amount_minor,
            approval_request_id=binding.approval_request_id, status="pending", created_at=context.business_time,
        ))


@pytest.fixture
def case(tmp_path: Path) -> dict:
    data = PROJECT_ROOT / "data/business/v1"
    records = check_contracts(data)["records"]
    context = ExecutionContext.model_validate(records["context"])
    proposal = ActionProposal.model_validate(records["proposal"])
    path = tmp_path / "business.sqlite3"
    initialize_database(path)
    with transaction(path) as connection:
        for order in json.loads((data / "orders.json").read_text(encoding="utf-8")):
            BusinessRepository(connection).add_order(Order.model_validate(order))
        tasks = TaskRepository(connection)
        tasks.add_task(Task.model_validate(records["task"]))
        tasks.add_attempt(Attempt.model_validate(records["attempt"]))
    return {
        "path": path, "context": context, "proposal": proposal,
        "service": BusinessService(path, BusinessRules.from_file(data / "spec.json")),
    }


def snapshot(case: dict) -> tuple:
    """Read business and authorization state through a separate connection."""
    with connect(case["path"]) as connection:
        return tuple(
            [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("orders", "approvals", "operations")
        )


def refuse(case: dict, action, context, code: str, **kwargs) -> None:
    before = snapshot(case)
    with pytest.raises(BusinessError) as error:
        case["service"].execute(action, context, **kwargs)
    assert error.value.code == code
    assert snapshot(case) == before


def revised(case: dict, action, *, suffix: str = "next") -> tuple:
    original = case["proposal"]
    proposal = ActionProposal.model_validate(original.model_dump() | {
        "proposal_id": f"proposal-{suffix}", "parameters": action.model_dump(),
    })
    context = ExecutionContext.model_validate(case["context"].model_dump() | {
        "write_binding": {
            "proposal_id": proposal.proposal_id, "proposal_version": proposal.proposal_version,
            "approval_request_id": f"approval-{suffix}", "operation_key": f"operation-{suffix}",
        },
    })
    return proposal, context


def test_refund_commits_balance_approval_and_ledger(case: dict) -> None:
    context, proposal = case["context"], case["proposal"]
    prepare(case["path"], context, proposal)
    result = case["service"].execute(proposal.parameters, context)
    assert result.status == "succeeded"
    assert result.amount_minor == 12900
    assert result.committed_at == context.business_time
    assert result.result["rule_id"] == "R-REFUND-01"
    with connect(case["path"]) as connection:
        assert BusinessRepository(connection).get_order("ORD-1001", context.owner_id).refunded_amount_minor == 12900
        assert BusinessRepository(connection).get_operation(result.operation_key, context.owner_id) == result
        assert TaskRepository(connection).approval_consumed_at(
            context.task_id, context.write_binding.approval_request_id, context.owner_id,
        ) == context.business_time


def test_same_key_returns_committed_result_after_expiry_and_cancellation(case: dict) -> None:
    context, proposal = case["context"], case["proposal"]
    prepare(case["path"], context, proposal)
    result = case["service"].execute(proposal.parameters, context)
    with transaction(case["path"]) as connection:
        TaskRepository(connection).set_status(context.task_id, context.owner_id, TaskStatus.CANCELLED)
    before = snapshot(case)
    late = ExecutionContext.model_validate(context.model_dump() | {
        "business_time": context.business_time + timedelta(days=30),
    })
    assert case["service"].execute(proposal.parameters, late) == result
    assert snapshot(case) == before


def test_unconfirmed_and_unprepared_calls_do_not_write(case: dict) -> None:
    context, proposal = case["context"], case["proposal"]
    no_binding = ExecutionContext.model_validate(context.model_dump() | {"write_binding": None})
    refuse(case, proposal.parameters, no_binding, "confirmation_required")
    refuse(case, proposal.parameters, context, "proposal_mismatch")
    prepare(case["path"], context, proposal)
    unprepared = ExecutionContext.model_validate(context.model_dump() | {
        "write_binding": context.write_binding.model_dump() | {"operation_key": "never-prepared"},
    })
    refuse(case, proposal.parameters, unprepared, "operation_not_prepared")


@pytest.mark.parametrize("field,value,code", [
    ("owner_id", "other-user", "task_not_found"),
    ("thread_id", "other-thread", "attempt_identity_mismatch"),
    ("model_version", "other-model", "attempt_identity_mismatch"),
    ("config_version", "other-config", "attempt_identity_mismatch"),
])
def test_context_identity_is_rechecked(case: dict, field: str, value: str, code: str) -> None:
    prepare(case["path"], case["context"], case["proposal"])
    changed = ExecutionContext.model_validate(case["context"].model_dump() | {field: value})
    refuse(case, case["proposal"].parameters, changed, code)


@pytest.mark.parametrize("change", [
    {"amount_minor": 12000}, {"order_id": "ORD-1002"}, {"reason": "changed reason"},
])
def test_actual_parameters_must_equal_confirmed_snapshot(case: dict, change: dict) -> None:
    action = case["proposal"].parameters
    prepare(case["path"], case["context"], case["proposal"])
    changed = type(action).model_validate(action.model_dump() | change)
    refuse(case, changed, case["context"], "proposal_mismatch")


@pytest.mark.parametrize("status", ["queued", "waiting_input", "completed", "failed", "cancelled"])
def test_nonexecuting_tasks_cannot_write(case: dict, status: str) -> None:
    context = case["context"]
    prepare(case["path"], context, case["proposal"])
    with transaction(case["path"]) as connection:
        TaskRepository(connection).set_status(context.task_id, context.owner_id, TaskStatus(status))
    refuse(case, case["proposal"].parameters, context, "task_not_executable")


def test_obsolete_attempt_cannot_write(case: dict) -> None:
    context = case["context"]
    prepare(case["path"], context, case["proposal"])
    with transaction(case["path"]) as connection:
        tasks = TaskRepository(connection)
        tasks.add_attempt(Attempt(
            **{name: getattr(context, name) for name in ("task_id", "owner_id", "model_version", "config_version")},
            attempt_id="new-attempt", thread_id="new-thread", created_at=context.business_time,
        ))
        tasks.set_current_attempt(context.task_id, context.owner_id, "new-attempt")
    refuse(case, case["proposal"].parameters, context, "attempt_not_current")


@pytest.mark.parametrize("new_id", [False, True])
def test_new_proposal_requires_new_confirmation(case: dict, new_id: bool) -> None:
    proposal, context = case["proposal"], case["context"]
    prepare(case["path"], context, proposal)
    newer = ActionProposal.model_validate(proposal.model_dump() | {
        "proposal_id": "replacement" if new_id else proposal.proposal_id,
        "proposal_version": 1 if new_id else 2,
    })
    with transaction(case["path"]) as connection:
        TaskRepository(connection).add_proposal(newer, context.owner_id)
    refuse(case, proposal.parameters, context, "proposal_not_current")


def test_rejection_and_consumed_confirmation_cannot_write(case: dict) -> None:
    context, proposal = case["context"], case["proposal"]
    prepare(case["path"], context, proposal, decision="rejected")
    refuse(case, proposal.parameters, context, "confirmation_required")
    proposal, context = revised(case, proposal.parameters)
    prepare(case["path"], context, proposal)
    with transaction(case["path"]) as connection:
        TaskRepository(connection).consume_approval(
            context.task_id, context.write_binding.approval_request_id, context.owner_id, context.business_time,
        )
    refuse(case, proposal.parameters, context, "confirmation_consumed")


def test_expired_proposal_and_future_approval_cannot_write(case: dict) -> None:
    context = case["context"]
    expires = context.business_time + timedelta(minutes=1)
    proposal = ActionProposal.model_validate(case["proposal"].model_dump() | {"expires_at": expires})
    prepare(case["path"], context, proposal)
    expired = ExecutionContext.model_validate(context.model_dump() | {"business_time": expires})
    refuse(case, proposal.parameters, expired, "proposal_expired")
    early = ExecutionContext.model_validate(context.model_dump() | {"business_time": context.business_time - timedelta(seconds=1)})
    refuse(case, proposal.parameters, early, "authorization_time_mismatch")


def test_order_ownership_is_rechecked(case: dict) -> None:
    with transaction(case["path"]) as connection:
        connection.execute("UPDATE orders SET owner_id = 'other-owner' WHERE order_id = 'ORD-1001'")
    before = snapshot(case)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        prepare(case["path"], case["context"], case["proposal"])
    assert snapshot(case) == before


def test_persisted_operation_must_match_approval_and_amount(case: dict) -> None:
    context = case["context"]
    prepare(case["path"], context, case["proposal"])
    with transaction(case["path"]) as connection:
        connection.execute("UPDATE operations SET amount_minor = 12000")
    refuse(case, case["proposal"].parameters, context, "operation_binding_mismatch")


def test_other_proposal_approval_cannot_authorize_original_action(case: dict) -> None:
    context, proposal = case["context"], case["proposal"]
    prepare(case["path"], context, proposal)
    other_proposal, other_context = revised(case, proposal.parameters)
    prepare(case["path"], other_context, other_proposal)
    mixed = ExecutionContext.model_validate(context.model_dump() | {
        "write_binding": context.write_binding.model_dump() | {
            "approval_request_id": other_context.write_binding.approval_request_id,
        },
    })
    refuse(case, proposal.parameters, mixed, "confirmation_mismatch")


def test_failed_operation_cannot_be_reexecuted(case: dict) -> None:
    context, proposal = case["context"], case["proposal"]
    prepare(case["path"], context, proposal)
    with transaction(case["path"]) as connection:
        BusinessRepository(connection).finish_operation(
            context.write_binding.operation_key, context.owner_id,
            status="failed", committed_at=None, result={"code": "explicit_failure"},
        )
    refuse(case, proposal.parameters, context, "operation_not_pending")


def test_cancelled_attempt_cannot_write(case: dict) -> None:
    context, proposal = case["context"], case["proposal"]
    prepare(case["path"], context, proposal)
    with transaction(case["path"]) as connection:
        connection.execute("UPDATE attempts SET status = 'cancelled' WHERE attempt_id = ?", (context.attempt_id,))
    refuse(case, proposal.parameters, context, "task_not_executable")


def test_business_rejection_leaves_confirmation_and_pending_record_untouched(case: dict) -> None:
    action = type(case["proposal"].parameters).model_validate(case["proposal"].parameters.model_dump() | {"amount_minor": 12000})
    proposal, context = revised(case, action)
    prepare(case["path"], context, proposal)
    refuse(case, action, context, "amount_mismatch")
    expired = ExecutionContext.model_validate(context.model_dump() | {"business_time": context.business_time + timedelta(days=30)})
    refuse(case, action, expired, "policy_not_active")


def test_ledger_failure_rolls_back_balance_and_confirmation(case: dict, monkeypatch) -> None:
    context, proposal = case["context"], case["proposal"]
    prepare(case["path"], context, proposal)
    before = snapshot(case)

    def fail(self, *args, **kwargs):
        assert self.get_order("ORD-1001", context.owner_id).refunded_amount_minor == 12900
        assert TaskRepository(self.connection).approval_consumed_at(
            context.task_id, context.write_binding.approval_request_id, context.owner_id,
        ) is not None
        raise RuntimeError("ledger write failed")

    monkeypatch.setattr(BusinessRepository, "finish_operation", fail)
    with pytest.raises(RuntimeError, match="ledger write failed"):
        case["service"].execute(proposal.parameters, context)
    assert snapshot(case) == before


def test_changed_key_with_new_confirmation_cannot_duplicate_refund(case: dict) -> None:
    context, proposal = case["context"], case["proposal"]
    prepare(case["path"], context, proposal)
    case["service"].execute(proposal.parameters, context)
    newer, changed = revised(case, proposal.parameters)
    prepare(case["path"], changed, newer)
    refuse(case, newer.parameters, changed, "business_action_already_committed")
    with connect(case["path"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operations WHERE status = 'succeeded'").fetchone()[0] == 1


def test_refund_and_coupon_commit_independently_and_preserve_each_other(case: dict) -> None:
    rules = case["service"].rules
    refund = type(case["proposal"].parameters).model_validate(case["proposal"].parameters.model_dump() | {
        "order_id": "ORD-1002", "amount_minor": 25900,
    })
    proposal, context = revised(case, refund, suffix="refund")
    prepare(case["path"], context, proposal)
    case["service"].execute(refund, context)
    coupon = CouponAction(order_id="ORD-1002", amount_minor=500, reason="delayed delivery", policy_refs=(rules.reference("issue_coupon"),))
    proposal, context = revised(case, coupon, suffix="coupon")
    prepare(case["path"], context, proposal)
    assert case["service"].execute(coupon, context).amount_minor == 500
    proposal, context = revised(case, coupon, suffix="duplicate-coupon")
    prepare(case["path"], context, proposal)
    refuse(case, coupon, context, "business_action_already_committed")
    with connect(case["path"]) as connection:
        order = BusinessRepository(connection).get_order("ORD-1002", context.owner_id)
        assert (order.refunded_amount_minor, order.coupon_amount_minor) == (25900, 500)
        assert connection.execute("SELECT COUNT(*) FROM operations WHERE status = 'succeeded'").fetchone()[0] == 2


@pytest.mark.parametrize("unresolved", [False, True])
def test_handoff_preserves_money_and_deduplicates_same_issue(case: dict, unresolved: bool) -> None:
    handoff = HandoffAction(
        order_id=None if unresolved else "ORD-1004",
        reason="order_unresolved" if unresolved else "unsupported_category", summary="manual review required",
        policy_refs=(case["service"].rules.reference("create_handoff"),),
    )
    proposal, context = revised(case, handoff)
    prepare(case["path"], context, proposal)
    if unresolved:
        refuse(case, handoff, context, "clarification_required")
    before_orders = snapshot(case)[0]
    result = case["service"].execute(handoff, context, clarification_attempted=unresolved)
    assert result.status == "succeeded"
    assert result.amount_minor is None
    assert result.result["summary"] == handoff.summary
    assert snapshot(case)[0] == before_orders
    proposal, context = revised(case, handoff, suffix="repeat")
    prepare(case["path"], context, proposal)
    refuse(case, handoff, context, "business_action_already_committed", clarification_attempted=unresolved)
