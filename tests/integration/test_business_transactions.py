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


def prepare(
    path: Path, context: ExecutionContext, proposal: ActionProposal, *,
    decision: str = "approved", persist_operation: bool = True,
) -> None:
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
        if not persist_operation:
            return
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


def test_public_prepare_persists_before_call_and_reuses_pending_key(case: dict) -> None:
    context, proposal = case["context"], case["proposal"]
    prepare(case["path"], context, proposal, persist_operation=False)
    before = snapshot(case)
    service = case["service"]
    pending = service.prepare(proposal.parameters, context)
    assert pending.status == "pending"
    assert pending.operation_key == context.write_binding.operation_key
    assert snapshot(case)[:2] == before[:2]
    read_context = ExecutionContext.model_validate(context.model_dump() | {"write_binding": None})
    fresh_service = BusinessService(case["path"], service.rules)
    assert fresh_service.get_operation(pending.operation_key, read_context) == pending
    later = ExecutionContext.model_validate(context.model_dump() | {
        "business_time": context.business_time + timedelta(minutes=1),
    })
    assert fresh_service.prepare(proposal.parameters, later) == pending
    assert len(snapshot(case)[2]) == 1
    result = fresh_service.execute(proposal.parameters, later)
    assert result.status == "succeeded"
    assert result.amount_minor == 12900
    assert fresh_service.get_operation(pending.operation_key, read_context) == result


@pytest.mark.parametrize("terminal", ["pending", "succeeded", "failed"])
def test_same_confirmation_cannot_switch_operation_key(case: dict, terminal: str) -> None:
    context, proposal = case["context"], case["proposal"]
    prepare(case["path"], context, proposal, persist_operation=False)
    service = case["service"]
    original = service.prepare(proposal.parameters, context)
    if terminal == "succeeded":
        original = service.execute(proposal.parameters, context)
    elif terminal == "failed":
        with transaction(case["path"]) as connection:
            BusinessRepository(connection).finish_operation(
                original.operation_key, context.owner_id, status="failed",
                committed_at=None, result={"code": "explicit_failure"},
            )
        original = service.get_operation(original.operation_key, context)
    before = snapshot(case)
    changed = ExecutionContext.model_validate(context.model_dump() | {
        "write_binding": context.write_binding.model_dump() | {"operation_key": "changed-key"},
    })
    with pytest.raises(BusinessError, match="approval_already_bound"):
        service.prepare(proposal.parameters, changed)
    assert snapshot(case) == before
    assert service.prepare(proposal.parameters, context) == original


def test_same_key_cannot_bind_a_different_confirmed_proposal(case: dict) -> None:
    context, proposal = case["context"], case["proposal"]
    prepare(case["path"], context, proposal, persist_operation=False)
    service = case["service"]
    original = service.prepare(proposal.parameters, context)
    revised_proposal, revised_context = revised(case, proposal.parameters)
    revised_context = ExecutionContext.model_validate(revised_context.model_dump() | {
        "write_binding": revised_context.write_binding.model_dump() | {"operation_key": original.operation_key},
    })
    prepare(case["path"], revised_context, revised_proposal, persist_operation=False)
    before = snapshot(case)
    with pytest.raises(BusinessError, match="operation_binding_mismatch"):
        service.prepare(proposal.parameters, revised_context)
    assert snapshot(case) == before
    assert service.get_operation(original.operation_key, context) == original


@pytest.mark.parametrize("problem,code", [
    ("rejected", "confirmation_required"),
    ("changed_amount", "proposal_mismatch"),
    ("expired", "proposal_expired"),
    ("wrong_amount", "amount_mismatch"),
])
def test_prepare_refuses_invalid_authorization_or_business(case: dict, problem: str, code: str) -> None:
    context, proposal = case["context"], case["proposal"]
    if problem == "expired":
        proposal = ActionProposal.model_validate(proposal.model_dump() | {
            "created_at": context.business_time - timedelta(minutes=2),
            "expires_at": context.business_time,
        })
    if problem == "wrong_amount":
        proposal = ActionProposal.model_validate(proposal.model_dump() | {
            "parameters": proposal.parameters.model_dump() | {"amount_minor": 12000},
        })
    prepare(case["path"], context, proposal, persist_operation=False,
            decision="rejected" if problem == "rejected" else "approved")
    action = proposal.parameters
    if problem == "changed_amount":
        action = type(action).model_validate(action.model_dump() | {"amount_minor": 12000})
    before = snapshot(case)
    with pytest.raises(BusinessError, match=code):
        case["service"].prepare(action, context)
    assert snapshot(case) == before


@pytest.mark.parametrize("change,code", [
    ("damage", "unverified_damage"),
    ("cancel", "task_not_executable"),
    ("proposal", "proposal_not_current"),
])
def test_execute_rechecks_state_after_prepare(case: dict, change: str, code: str) -> None:
    context, proposal = case["context"], case["proposal"]
    prepare(case["path"], context, proposal, persist_operation=False)
    case["service"].prepare(proposal.parameters, context)
    with transaction(case["path"]) as connection:
        if change == "damage":
            connection.execute("UPDATE orders SET damage_verified = 0 WHERE order_id = 'ORD-1001'")
        elif change == "cancel":
            TaskRepository(connection).set_status(context.task_id, context.owner_id, TaskStatus.CANCELLED)
        else:
            TaskRepository(connection).add_proposal(ActionProposal.model_validate(
                proposal.model_dump() | {"proposal_version": 2},
            ), context.owner_id)
    refuse(case, proposal.parameters, context, code)


def test_prepare_insert_failure_rolls_back_key_and_preserves_confirmation(case: dict, monkeypatch) -> None:
    context, proposal = case["context"], case["proposal"]
    prepare(case["path"], context, proposal, persist_operation=False)
    before = snapshot(case)
    original = BusinessRepository.add_operation

    def fail(self, operation):
        original(self, operation)
        assert self.get_operation(operation.operation_key, operation.owner_id) is not None
        raise RuntimeError("prepare interrupted")

    monkeypatch.setattr(BusinessRepository, "add_operation", fail)
    with pytest.raises(RuntimeError, match="prepare interrupted"):
        case["service"].prepare(proposal.parameters, context)
    assert snapshot(case) == before


def test_query_is_owned_and_readable_after_cancel_without_write_binding(case: dict) -> None:
    context, proposal = case["context"], case["proposal"]
    prepare(case["path"], context, proposal, persist_operation=False)
    service = case["service"]
    pending = service.prepare(proposal.parameters, context)
    result = service.execute(proposal.parameters, context)
    foreign_task = Task(task_id="foreign-task", owner_id="foreign-owner",
                        user_message="other ticket", current_attempt_id="foreign-attempt",
                        created_at=context.business_time)
    foreign_attempt = Attempt(task_id=foreign_task.task_id, owner_id=foreign_task.owner_id,
                              attempt_id="foreign-attempt", thread_id="foreign-thread",
                              model_version="manual", config_version="contract-preview-v1",
                              created_at=context.business_time)
    with transaction(case["path"]) as connection:
        tasks = TaskRepository(connection)
        tasks.add_task(foreign_task)
        tasks.add_attempt(foreign_attempt)
        tasks.set_status(context.task_id, context.owner_id, TaskStatus.CANCELLED)
    foreign_context = ExecutionContext(**{
        name: getattr(foreign_attempt, name) for name in ExecutionContext.model_fields
        if name not in ("business_time", "write_binding")
    }, business_time=context.business_time)
    assert service.get_operation(pending.operation_key, foreign_context) is None
    assert service.get_operation("unknown-key", context) is None
    read_context = ExecutionContext.model_validate(context.model_dump() | {
        "write_binding": None, "business_time": context.business_time + timedelta(days=30),
    })
    before = snapshot(case)
    assert service.get_operation(pending.operation_key, read_context) == result
    assert snapshot(case) == before
    changed_identity = ExecutionContext.model_validate(read_context.model_dump() | {"thread_id": "wrong-thread"})
    with pytest.raises(BusinessError, match="attempt_identity_mismatch"):
        service.get_operation(pending.operation_key, changed_identity)


@pytest.mark.parametrize("action_name", ["request_refund", "issue_coupon", "create_handoff"])
def test_public_preparation_flow_deduplicates_new_key_and_confirmation(case: dict, action_name: str) -> None:
    service = case["service"]
    if action_name == "request_refund":
        action = case["proposal"].parameters
    elif action_name == "issue_coupon":
        action = CouponAction(order_id="ORD-1002", amount_minor=500, reason="delayed delivery",
                              policy_refs=(service.rules.reference(action_name),))
    else:
        action = HandoffAction(order_id="ORD-1004", reason="unsupported_category",
                               summary="manual review", policy_refs=(service.rules.reference(action_name),))
    proposal, context = revised(case, action, suffix="first")
    prepare(case["path"], context, proposal, persist_operation=False)
    pending = service.prepare(action, context)
    assert pending.status == "pending"
    result = service.execute(action, context)
    assert service.get_operation(pending.operation_key, context) == result
    other_proposal, other_context = revised(case, action, suffix="duplicate")
    prepare(case["path"], other_context, other_proposal, persist_operation=False)
    before = snapshot(case)
    with pytest.raises(BusinessError, match="business_action_already_committed"):
        service.prepare(action, other_context)
    assert snapshot(case) == before
    assert service.get_operation(other_context.write_binding.operation_key, context) is None
    assert len(snapshot(case)[2]) == 1


def test_prepare_cannot_reuse_another_owners_key(case: dict) -> None:
    context, proposal = case["context"], case["proposal"]
    foreign_context = ExecutionContext.model_validate(context.model_dump() | {
        "task_id": "foreign-task", "attempt_id": "foreign-attempt",
        "owner_id": "foreign-owner", "thread_id": "foreign-thread",
        "write_binding": context.write_binding.model_dump() | {
            "proposal_id": "foreign-proposal", "approval_request_id": "foreign-approval",
        },
    })
    foreign_proposal = ActionProposal.model_validate(proposal.model_dump() | {
        "task_id": foreign_context.task_id, "attempt_id": foreign_context.attempt_id,
        "proposal_id": foreign_context.write_binding.proposal_id,
        "parameters": proposal.parameters.model_dump() | {"order_id": "foreign-order"},
    })
    with transaction(case["path"]) as connection:
        business = BusinessRepository(connection)
        business.add_order(Order.model_validate(business.get_order("ORD-1001", context.owner_id).model_dump() | {
            "order_id": "foreign-order", "owner_id": foreign_context.owner_id,
        }))
        tasks = TaskRepository(connection)
        tasks.add_task(Task(task_id=foreign_context.task_id, owner_id=foreign_context.owner_id,
                            current_attempt_id=foreign_context.attempt_id, user_message="other ticket",
                            status="waiting_approval", created_at=context.business_time))
        tasks.add_attempt(Attempt(**{
            name: getattr(foreign_context, name) for name in ("task_id", "attempt_id", "owner_id", "thread_id", "model_version", "config_version")
        }, status="waiting_approval", created_at=context.business_time))
    prepare(case["path"], foreign_context, foreign_proposal)
    prepare(case["path"], context, proposal, persist_operation=False)
    before = snapshot(case)
    with pytest.raises(BusinessError, match="operation_key_conflict"):
        case["service"].prepare(proposal.parameters, context)
    assert snapshot(case) == before
    assert case["service"].get_operation(context.write_binding.operation_key, context) is None
