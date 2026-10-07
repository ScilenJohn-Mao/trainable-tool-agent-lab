"""Exercise typed repositories against isolated SQLite databases."""

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.check_contracts import check_contracts
from tool_agent_lab.schemas.actions import ActionProposal, Approval, ApprovalRequest, HandoffAction
from tool_agent_lab.schemas.business import Operation, Order
from tool_agent_lab.schemas.events import EventType
from tool_agent_lab.schemas.tasks import Attempt, Task, TaskStatus
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect, initialize_database, transaction
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository


@pytest.fixture
def records() -> dict:
    data = check_contracts(PROJECT_ROOT / "data/business/v1")["records"]
    orders = json.loads((PROJECT_ROOT / "data/business/v1/orders.json").read_text(encoding="utf-8"))
    approval = Approval.model_validate(data["approval"])
    return {
        "task": Task.model_validate(data["task"]),
        "attempt": Attempt.model_validate(data["attempt"]),
        "approval": approval,
        "order": Order.model_validate(orders[0]),
        "operation": Operation(
            operation_key="refund-key", task_id=approval.proposal.task_id,
            attempt_id=approval.proposal.attempt_id, owner_id=approval.owner_id,
            order_id="ORD-1001", action="request_refund", amount_minor=12900,
            approval_request_id=approval.request.request_id, status="pending",
            created_at=approval.decided_at,
        ),
    }


@pytest.fixture
def db_path(tmp_path: Path, records: dict) -> Path:
    path = tmp_path / "app.sqlite3"
    initialize_database(path)
    with transaction(path) as connection:
        BusinessRepository(connection).add_order(records["order"])
        tasks = TaskRepository(connection)
        tasks.add_task(records["task"])
        tasks.add_attempt(records["attempt"])
        tasks.add_proposal(records["approval"].proposal, records["task"].owner_id)
        tasks.add_approval(records["approval"])
    return path


def test_records_round_trip_across_connections_and_owner_scopes(db_path: Path, records: dict) -> None:
    task, attempt, approval, order = [records[name] for name in ("task", "attempt", "approval", "order")]
    with connect(db_path) as connection:
        tasks = TaskRepository(connection)
        assert tasks.get_task(task.task_id, task.owner_id) == task
        assert tasks.get_attempt(task.task_id, attempt.attempt_id, task.owner_id) == attempt
        assert tasks.get_proposal(task.task_id, approval.proposal.proposal_id, task.owner_id) == approval.proposal
        assert tasks.get_approval(task.task_id, approval.request.request_id, task.owner_id) == approval
        assert BusinessRepository(connection).get_order(order.order_id, task.owner_id) == order
        assert tasks.get_task(task.task_id, "other-owner") is None
        assert tasks.get_attempt(task.task_id, attempt.attempt_id, "other-owner") is None
        assert tasks.get_proposal(task.task_id, approval.proposal.proposal_id, "other-owner") is None
        assert tasks.get_approval(task.task_id, approval.request.request_id, "other-owner") is None
        assert BusinessRepository(connection).get_order(order.order_id, "other-owner") is None
        assert tasks.list_tasks("other-owner") == []


def test_owned_task_status_filter_and_pagination(db_path: Path, records: dict) -> None:
    original = records["task"]
    newer = Task.model_validate(original.model_dump() | {
        "task_id": "newer", "current_attempt_id": None, "status": "queued",
        "created_at": original.created_at + timedelta(minutes=1),
    })
    unrelated = Task.model_validate(newer.model_dump() | {"task_id": "unrelated", "owner_id": "other-owner"})
    with transaction(db_path) as connection:
        tasks = TaskRepository(connection)
        tasks.add_task(newer)
        tasks.add_task(unrelated)
        assert tasks.set_status(original.task_id, original.owner_id, TaskStatus.RUNNING)
        assert tasks.set_current_attempt(original.task_id, original.owner_id, records["attempt"].attempt_id)
        assert not tasks.set_status(original.task_id, "other-owner", TaskStatus.CANCELLED)
        assert not tasks.set_current_attempt(original.task_id, "other-owner", "missing")
    with connect(db_path) as connection:
        tasks = TaskRepository(connection)
        assert [task.task_id for task in tasks.list_tasks(original.owner_id)] == ["newer", original.task_id]
        assert [task.task_id for task in tasks.list_tasks(original.owner_id, limit=1, offset=1)] == [original.task_id]
        assert tasks.list_tasks(original.owner_id, status=TaskStatus.QUEUED) == [newer]
        assert tasks.get_task(original.task_id, original.owner_id).status == TaskStatus.RUNNING


def test_proposal_versions_keep_original_approval_snapshot(db_path: Path, records: dict) -> None:
    approval = records["approval"]
    original = approval.proposal
    revised = ActionProposal.model_validate(original.model_dump() | {
        "proposal_version": 2,
        "parameters": original.parameters.model_dump() | {"amount_minor": 12000, "reason": "修改金额"},
    })
    with transaction(db_path) as connection:
        tasks = TaskRepository(connection)
        tasks.add_proposal(revised, approval.owner_id)
    with connect(db_path) as connection:
        tasks = TaskRepository(connection)
        assert tasks.get_proposal(original.task_id, original.proposal_id, approval.owner_id) == revised
        assert tasks.get_proposal(original.task_id, original.proposal_id, approval.owner_id, version=1) == original
        assert tasks.get_approval(original.task_id, approval.request.request_id, approval.owner_id) == approval
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(db_path) as connection:
            TaskRepository(connection).add_proposal(revised, approval.owner_id)


def test_repositories_reject_foreign_proposal_and_changed_approval_snapshot(db_path: Path, records: dict) -> None:
    approval = records["approval"]
    with pytest.raises(ValueError, match="belong to owner"):
        with transaction(db_path) as connection:
            TaskRepository(connection).add_proposal(approval.proposal, "other-owner")
    changed = Approval.model_validate(approval.model_dump() | {
        "proposal": approval.proposal.model_dump() | {
            "parameters": approval.proposal.parameters.model_dump() | {"amount_minor": 12000},
        },
    })
    with pytest.raises(ValueError, match="snapshot does not match"):
        with transaction(db_path) as connection:
            TaskRepository(connection).add_approval(changed)


def test_approval_consumption_is_owned_and_once_only(db_path: Path, records: dict) -> None:
    approval = records["approval"]
    args = (approval.proposal.task_id, approval.request.request_id, approval.owner_id)
    with transaction(db_path) as connection:
        tasks = TaskRepository(connection)
        assert not tasks.consume_approval(args[0], args[1], "other-owner", approval.decided_at)
        assert not tasks.consume_approval("other-task", args[1], args[2], approval.decided_at)
        assert tasks.consume_approval(*args, approval.decided_at)
        assert not tasks.consume_approval(*args, approval.decided_at)
    with connect(db_path) as connection:
        tasks = TaskRepository(connection)
        assert tasks.approval_consumed_at(*args) == approval.decided_at
        assert tasks.approval_consumed_at(args[0], args[1], "other-owner") is None
        assert tasks.get_approval(*args) == approval


def test_rejected_confirmation_cannot_be_consumed(db_path: Path, records: dict) -> None:
    approval = records["approval"]
    proposal = ActionProposal.model_validate(approval.proposal.model_dump() | {"proposal_version": 2})
    rejected = Approval(
        request=ApprovalRequest(request_id="rejected", proposal_id=proposal.proposal_id,
            proposal_version=2, decision="rejected"),
        proposal=proposal, owner_id=approval.owner_id, decided_at=approval.decided_at,
    )
    with transaction(db_path) as connection:
        tasks = TaskRepository(connection)
        tasks.add_proposal(proposal, approval.owner_id)
        tasks.add_approval(rejected)
        assert not tasks.consume_approval(proposal.task_id, "rejected", approval.owner_id, approval.decided_at)


def test_pending_operation_survives_connection_and_hides_other_owners(db_path: Path, records: dict) -> None:
    operation = records["operation"]
    with transaction(db_path) as connection:
        BusinessRepository(connection).add_operation(operation)
    with connect(db_path) as connection:
        business = BusinessRepository(connection)
        assert business.get_operation(operation.operation_key, operation.owner_id) == operation
        assert business.get_operation(operation.operation_key, "other-owner") is None
        assert business.get_successful_operation(operation.order_id, operation.action, operation.owner_id) is None
    with transaction(db_path) as connection:
        business = BusinessRepository(connection)
        assert not business.finish_operation(operation.operation_key, "other-owner",
            status="failed", committed_at=None, result={"error": "denied"})
        assert not business.set_balances(operation.order_id, "other-owner",
            refunded_amount_minor=12900, coupon_amount_minor=0)


def apply_stored_refund(connection: sqlite3.Connection, records: dict) -> None:
    approval, operation = records["approval"], records["operation"]
    business, tasks, events = [factory(connection) for factory in (BusinessRepository, TaskRepository, EventRepository)]
    assert tasks.consume_approval(approval.proposal.task_id, approval.request.request_id,
        approval.owner_id, approval.decided_at)
    assert business.set_balances(operation.order_id, operation.owner_id,
        refunded_amount_minor=12900, coupon_amount_minor=0)
    assert business.finish_operation(operation.operation_key, operation.owner_id,
        status="succeeded", committed_at=approval.decided_at, result={"message": "已登记", "amount_minor": 12900})
    events.append(records["attempt"], EventType.TOOL_RESULT, approval.decided_at,
        call_id="refund-call", payload={"operation_key": operation.operation_key})


def test_shared_transaction_rolls_back_then_commits_all_repositories(db_path: Path, records: dict) -> None:
    operation, approval = records["operation"], records["approval"]
    with transaction(db_path) as connection:
        BusinessRepository(connection).add_operation(operation)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        with transaction(db_path) as connection:
            apply_stored_refund(connection, records)
            raise RuntimeError("simulated interruption")
    with connect(db_path) as connection:
        assert BusinessRepository(connection).get_order(operation.order_id, operation.owner_id).refunded_amount_minor == 0
        assert BusinessRepository(connection).get_operation(operation.operation_key, operation.owner_id) == operation
        assert TaskRepository(connection).approval_consumed_at(approval.proposal.task_id,
            approval.request.request_id, approval.owner_id) is None
        assert EventRepository(connection).list_events(approval.proposal.task_id, approval.owner_id) == []
    with transaction(db_path) as connection:
        apply_stored_refund(connection, records)
    with connect(db_path) as connection:
        business = BusinessRepository(connection)
        stored = business.get_successful_operation(operation.order_id, operation.action, operation.owner_id)
        assert stored.operation_key == operation.operation_key
        assert stored.status == "succeeded" and stored.amount_minor == 12900
        assert stored.result == {"message": "已登记", "amount_minor": 12900}
        assert business.get_order(operation.order_id, operation.owner_id).refunded_amount_minor == 12900
        assert TaskRepository(connection).approval_consumed_at(approval.proposal.task_id,
            approval.request.request_id, approval.owner_id) == approval.decided_at
        assert len(EventRepository(connection).list_events(approval.proposal.task_id, approval.owner_id)) == 1
    with transaction(db_path) as connection:
        assert not BusinessRepository(connection).finish_operation(operation.operation_key, operation.owner_id,
            status="failed", committed_at=None, result={"error": "late response"})


def test_changed_operation_key_still_cannot_duplicate_success(db_path: Path, records: dict) -> None:
    original, approval = records["operation"], records["approval"]
    revised = ActionProposal.model_validate(approval.proposal.model_dump() | {"proposal_version": 2})
    second_approval = Approval(
        request=ApprovalRequest(request_id="another-approval", proposal_id=revised.proposal_id,
            proposal_version=2, decision="approved"),
        proposal=revised, owner_id=approval.owner_id, decided_at=approval.decided_at,
    )
    second = Operation.model_validate(original.model_dump() | {
        "operation_key": "changed-key", "approval_request_id": "another-approval",
    })
    with transaction(db_path) as connection:
        business, tasks = BusinessRepository(connection), TaskRepository(connection)
        business.add_operation(original)
        apply_stored_refund(connection, records)
        tasks.add_proposal(revised, approval.owner_id)
        tasks.add_approval(second_approval)
        business.add_operation(second)
    with pytest.raises(sqlite3.IntegrityError, match="operations.order_id, operations.action"):
        with transaction(db_path) as connection:
            TaskRepository(connection).consume_approval(revised.task_id, "another-approval",
                approval.owner_id, approval.decided_at)
            BusinessRepository(connection).set_balances(original.order_id, original.owner_id,
                refunded_amount_minor=12900, coupon_amount_minor=500)
            BusinessRepository(connection).finish_operation("changed-key", original.owner_id,
                status="succeeded", committed_at=approval.decided_at, result={})
    with connect(db_path) as connection:
        business = BusinessRepository(connection)
        assert business.get_operation("changed-key", original.owner_id) == second
        assert business.get_order(original.order_id, original.owner_id).coupon_amount_minor == 0
        assert TaskRepository(connection).approval_consumed_at(revised.task_id,
            "another-approval", original.owner_id) is None


def test_refund_and_coupon_can_commit_independently(db_path: Path, records: dict) -> None:
    approval, original = records["approval"], records["operation"]
    proposal = ActionProposal.model_validate(approval.proposal.model_dump() | {
        "proposal_version": 2,
        "parameters": approval.proposal.parameters.model_dump() | {
            "action": "issue_coupon", "amount_minor": 500, "reason": "延误补偿",
        },
    })
    coupon_approval = Approval(
        request=ApprovalRequest(request_id="coupon-approval", proposal_id=proposal.proposal_id,
            proposal_version=2, decision="approved"),
        proposal=proposal, owner_id=approval.owner_id, decided_at=approval.decided_at,
    )
    coupon = Operation.model_validate(original.model_dump() | {
        "operation_key": "coupon", "action": "issue_coupon", "amount_minor": 500,
        "approval_request_id": "coupon-approval",
    })
    with transaction(db_path) as connection:
        business, tasks = BusinessRepository(connection), TaskRepository(connection)
        business.add_operation(original)
        apply_stored_refund(connection, records)
        tasks.add_proposal(proposal, approval.owner_id)
        tasks.add_approval(coupon_approval)
        business.add_operation(coupon)
        assert tasks.consume_approval(proposal.task_id, "coupon-approval", approval.owner_id, approval.decided_at)
        assert business.set_balances(original.order_id, original.owner_id,
            refunded_amount_minor=12900, coupon_amount_minor=500)
        assert business.finish_operation("coupon", original.owner_id,
            status="succeeded", committed_at=approval.decided_at, result={"amount_minor": 500})
    with connect(db_path) as connection:
        business = BusinessRepository(connection)
        order = business.get_order(original.order_id, original.owner_id)
        assert (order.refunded_amount_minor, order.coupon_amount_minor) == (12900, 500)
        assert business.get_successful_operation(original.order_id, "request_refund", original.owner_id).amount_minor == 12900
        assert business.get_successful_operation(original.order_id, "issue_coupon", original.owner_id).amount_minor == 500
        assert business.get_successful_operation(original.order_id, "issue_coupon", "other-owner") is None


def test_finish_requires_terminal_status_and_consumption_requires_timezone(db_path: Path, records: dict) -> None:
    operation, approval = records["operation"], records["approval"]
    with transaction(db_path) as connection:
        BusinessRepository(connection).add_operation(operation)
    with pytest.raises(ValueError, match="terminal status"):
        with transaction(db_path) as connection:
            BusinessRepository(connection).finish_operation(operation.operation_key, operation.owner_id,
                status="pending", committed_at=None, result={})
    with pytest.raises(ValueError, match="timezone"):
        with transaction(db_path) as connection:
            TaskRepository(connection).consume_approval(approval.proposal.task_id,
                approval.request.request_id, approval.owner_id, approval.decided_at.replace(tzinfo=None))
    with connect(db_path) as connection:
        assert BusinessRepository(connection).get_operation(operation.operation_key, operation.owner_id) == operation
        assert TaskRepository(connection).approval_consumed_at(approval.proposal.task_id,
            approval.request.request_id, approval.owner_id) is None


@pytest.mark.parametrize("amount", [True, 12.5, -1, 12901])
def test_invalid_balances_do_not_write(db_path: Path, records: dict, amount: object) -> None:
    order = records["order"]
    with pytest.raises(ValidationError):
        with transaction(db_path) as connection:
            BusinessRepository(connection).set_balances(order.order_id, order.owner_id,
                refunded_amount_minor=amount, coupon_amount_minor=0)
    with connect(db_path) as connection:
        assert BusinessRepository(connection).get_order(order.order_id, order.owner_id) == order


@pytest.mark.parametrize("repository", ["task", "business", "event"])
def test_writes_require_caller_transaction(db_path: Path, records: dict, repository: str) -> None:
    with connect(db_path) as connection:
        with pytest.raises(RuntimeError, match="explicit transaction"):
            if repository == "task":
                TaskRepository(connection).set_status(records["task"].task_id,
                    records["task"].owner_id, TaskStatus.RUNNING)
            elif repository == "business":
                BusinessRepository(connection).add_operation(records["operation"])
            else:
                EventRepository(connection).append(records["attempt"], EventType.TASK_STATUS_CHANGED,
                    records["approval"].decided_at)


def test_events_resume_per_task_across_attempts_and_do_not_leak(db_path: Path, records: dict) -> None:
    attempt, approval = records["attempt"], records["approval"]
    with transaction(db_path) as connection:
        events = EventRepository(connection)
        first = events.append(attempt, EventType.TOOL_CALL, approval.decided_at,
            call_id="call-1", payload={"message": "退款查询"})
        second = events.append(attempt, EventType.TOOL_RESULT, approval.decided_at,
            call_id="call-1", payload={"amount_minor": 12900})
        assert (first.seq, second.seq) == (1, 2)
    new_attempt = Attempt.model_validate(attempt.model_dump() | {
        "attempt_id": "new-attempt", "thread_id": "new-thread", "model_version": "manual-new",
    })
    other_task = Task.model_validate(records["task"].model_dump() | {
        "task_id": "other-task", "current_attempt_id": None,
    })
    other_attempt = Attempt.model_validate(attempt.model_dump() | {
        "task_id": "other-task", "attempt_id": "other-attempt", "thread_id": "other-thread",
    })
    with transaction(db_path) as connection:
        tasks, events = TaskRepository(connection), EventRepository(connection)
        tasks.add_attempt(new_attempt)
        tasks.add_task(other_task)
        tasks.add_attempt(other_attempt)
        assert events.append(new_attempt, EventType.TASK_STATUS_CHANGED, approval.decided_at).seq == 3
        assert events.append(other_attempt, EventType.TASK_STATUS_CHANGED, approval.decided_at).seq == 1
    with connect(db_path) as connection:
        events = EventRepository(connection)
        assert events.list_events(attempt.task_id, attempt.owner_id, limit=1) == [first]
        assert events.list_events(attempt.task_id, attempt.owner_id, after_seq=1, limit=1) == [second]
        assert [event.seq for event in events.list_events(attempt.task_id, attempt.owner_id, after_seq=2)] == [3]
        assert events.list_events(attempt.task_id, "other-owner") == []
        assert events.list_events("missing", attempt.owner_id) == []


def test_invalid_event_identity_or_payload_does_not_consume_sequence(db_path: Path, records: dict) -> None:
    attempt, now = records["attempt"], records["approval"].decided_at
    invalid = Attempt.model_validate(attempt.model_dump() | {"model_version": "wrong-version"})
    with pytest.raises(ValueError, match="persisted attempt"):
        with transaction(db_path) as connection:
            EventRepository(connection).append(invalid, EventType.TASK_STATUS_CHANGED, now)
    with pytest.raises(ValidationError, match="call_id"):
        with transaction(db_path) as connection:
            EventRepository(connection).append(attempt, EventType.TOOL_CALL, now)
    with pytest.raises(ValidationError, match="UTF-8 bytes"):
        with transaction(db_path) as connection:
            EventRepository(connection).append(attempt, EventType.TASK_STATUS_CHANGED,
                now, payload={"text": "中" * 6000})
    with transaction(db_path) as connection:
        assert EventRepository(connection).append(attempt, EventType.TASK_STATUS_CHANGED, now).seq == 1


def test_order_unresolved_handoff_and_failed_operation_records(db_path: Path, records: dict) -> None:
    approval = records["approval"]
    proposal = ActionProposal.model_validate(approval.proposal.model_dump() | {
        "proposal_version": 2,
        "parameters": HandoffAction(reason="order_unresolved", summary="缺少订单号",
            policy_refs=approval.proposal.parameters.policy_refs),
    })
    handoff_approval = Approval(
        request=ApprovalRequest(request_id="handoff-approval", proposal_id=proposal.proposal_id,
            proposal_version=2, decision="approved"),
        proposal=proposal, owner_id=approval.owner_id, decided_at=approval.decided_at,
    )
    operation = Operation.model_validate(records["operation"].model_dump() | {
        "operation_key": "handoff", "action": "create_handoff", "order_id": None,
        "amount_minor": None, "approval_request_id": "handoff-approval",
    })
    with transaction(db_path) as connection:
        tasks, business = TaskRepository(connection), BusinessRepository(connection)
        tasks.add_proposal(proposal, approval.owner_id)
        tasks.add_approval(handoff_approval)
        business.add_operation(operation)
        assert business.finish_operation("handoff", approval.owner_id,
            status="failed", committed_at=None, result={"error": "明确失败"})
    with connect(db_path) as connection:
        stored = BusinessRepository(connection).get_operation("handoff", approval.owner_id)
        assert stored.action == "create_handoff" and stored.order_id is None
        assert stored.status == "failed" and stored.committed_at is None
        assert stored.result == {"error": "明确失败"}
