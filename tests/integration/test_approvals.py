"""Run task/proposal/human-decision services against isolated business databases."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.business.service import BusinessError, BusinessService
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.runtime.task_service import TaskError, TaskService
from tool_agent_lab.schemas.actions import ApprovalRequest, ExecutionContext, RefundAction, WriteBinding
from tool_agent_lab.schemas.business import Order
from tool_agent_lab.schemas.tasks import AttemptIdentity, TaskCreate, TaskStatus
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect, initialize_database, transaction
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository


@pytest.fixture
def case(tmp_path: Path) -> dict:
    path = tmp_path / "app.sqlite3"
    initialize_database(path)
    data = PROJECT_ROOT / "data/business/v1"
    with transaction(path) as connection:
        for values in json.loads((data / "orders.json").read_text(encoding="utf-8")):
            BusinessRepository(connection).add_order(Order.model_validate(values))
    rules = BusinessRules.from_file(data / "spec.json")
    tasks = TaskService(path, business_time=rules.business_time)
    task = tasks.create(TaskCreate(user_message="refund damaged order", order_id="ORD-1001"), owner_id="demo-user")
    attempt = tasks.start(task.task_id, owner_id=task.owner_id)
    identity = AttemptIdentity(**{name: getattr(attempt, name) for name in AttemptIdentity.model_fields})
    action = RefundAction(order_id="ORD-1001", amount_minor=12900, reason="verified damage",
                          policy_refs=(rules.reference("request_refund"),))
    return {
        "path": path, "task": task, "identity": identity, "action": action, "rules": rules,
        "tasks": tasks, "approvals": ApprovalService(path, business_time=rules.business_time),
        "business": BusinessService(path, rules),
    }


def request_for(proposal, *, decision: str = "approved", request_id: str = "user-response") -> ApprovalRequest:
    return ApprovalRequest(request_id=request_id, proposal_id=proposal.proposal_id,
                           proposal_version=proposal.proposal_version, decision=decision)


def context_for(case: dict, approval, *, operation_key: str = "refund-operation") -> ExecutionContext:
    return ExecutionContext(**case["identity"].model_dump(), business_time=case["rules"].business_time,
                            write_binding=WriteBinding(
                                proposal_id=approval.proposal.proposal_id,
                                proposal_version=approval.proposal.proposal_version,
                                approval_request_id=approval.request.request_id, operation_key=operation_key,
                            ))


def snapshot(case: dict) -> tuple:
    with connect(case["path"]) as connection:
        return tuple([tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                     for table in ("tasks", "attempts", "proposals", "approvals", "orders", "operations", "events"))


def test_create_start_and_owned_queries(case: dict) -> None:
    tasks, task = case["tasks"], case["task"]
    assert tasks.get(task.task_id, owner_id=task.owner_id).status == TaskStatus.RUNNING
    assert tasks.get(task.task_id, owner_id="other-owner") is None
    assert tasks.list(owner_id="other-owner") == []
    assert tasks.list(owner_id=task.owner_id, status=TaskStatus.RUNNING)[0].task_id == task.task_id
    before = snapshot(case)
    assert tasks.start(task.task_id, owner_id=task.owner_id).attempt_id == task.current_attempt_id
    assert snapshot(case) == before
    queued = tasks.create(TaskCreate(user_message="order missing"), owner_id=task.owner_id)
    assert queued.status == TaskStatus.QUEUED
    assert tasks.current_proposal(queued.task_id, owner_id=queued.owner_id) is None
    assert tasks.list(owner_id=task.owner_id, status=TaskStatus.QUEUED) == [queued]


def test_task_creation_cannot_reference_foreign_order(case: dict) -> None:
    before = snapshot(case)
    with pytest.raises(TaskError, match="order_not_found"):
        case["tasks"].create(TaskCreate(user_message="wrong owner", order_id="ORD-1001"), owner_id="other-owner")
    assert snapshot(case) == before


def test_approval_through_services_enables_single_refund(case: dict) -> None:
    task, tasks = case["task"], case["tasks"]
    proposal = tasks.propose(case["action"], case["identity"])
    assert tasks.get(task.task_id, owner_id=task.owner_id).status == TaskStatus.WAITING_APPROVAL
    assert tasks.current_proposal(task.task_id, owner_id=task.owner_id) == proposal
    assert tasks.current_proposal(task.task_id, owner_id="other-owner") is None
    request = request_for(proposal)
    approval = case["approvals"].record(task.task_id, request, owner_id=task.owner_id)
    assert approval.proposal == proposal
    assert approval.owner_id == task.owner_id
    with connect(case["path"]) as connection:
        repository = TaskRepository(connection)
        assert repository.approval_consumed_at(task.task_id, request.request_id, task.owner_id) is None
        assert repository.get_attempt(task.task_id, task.current_attempt_id, task.owner_id).status == TaskStatus.RUNNING
        assert BusinessRepository(connection).get_order("ORD-1001", task.owner_id).refunded_amount_minor == 0
    context = context_for(case, approval)
    pending = case["business"].prepare(case["action"], context)
    result = case["business"].execute(case["action"], context)
    assert result.amount_minor == 12900 and result.status == "succeeded"
    before = snapshot(case)
    assert case["approvals"].record(task.task_id, request, owner_id=task.owner_id) == approval
    assert case["business"].prepare(case["action"], context) == result
    assert case["business"].get_operation(pending.operation_key, context) == result
    assert snapshot(case) == before
    with connect(case["path"]) as connection:
        events = EventRepository(connection).list_events(task.task_id, task.owner_id)
        assert [event.seq for event in events] == list(range(1, 7))
        assert [event.event_type for event in events] == [
            "task_status_changed", "task_status_changed", "action_proposed",
            "task_status_changed", "approval_recorded", "task_status_changed",
        ]
        assert connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM operations WHERE status = 'succeeded'").fetchone()[0] == 1


def test_rejection_cannot_write_or_be_changed_to_approval(case: dict) -> None:
    task = case["task"]
    proposal = case["tasks"].propose(case["action"], case["identity"])
    request = request_for(proposal, decision="rejected")
    rejected = case["approvals"].record(task.task_id, request, owner_id=task.owner_id)
    before = snapshot(case)
    with pytest.raises(BusinessError, match="confirmation_required"):
        case["business"].prepare(case["action"], context_for(case, rejected))
    with pytest.raises(TaskError, match="confirmation_request_conflict"):
        case["approvals"].record(task.task_id, request_for(proposal), owner_id=task.owner_id)
    assert case["approvals"].record(task.task_id, request, owner_id=task.owner_id) == rejected
    assert snapshot(case) == before
    updated = case["tasks"].propose(case["action"], case["identity"])
    assert updated.proposal_version == 2
    accepted = case["approvals"].record(task.task_id, request_for(updated, request_id="new-response"), owner_id=task.owner_id)
    assert accepted.request.decision == "approved"


def test_changed_parameters_require_current_version_and_new_confirmation(case: dict) -> None:
    task, tasks = case["task"], case["tasks"]
    original = tasks.propose(case["action"], case["identity"])
    approval = case["approvals"].record(task.task_id, request_for(original), owner_id=task.owner_id)
    context = context_for(case, approval)
    case["business"].prepare(case["action"], context)
    changed = RefundAction.model_validate(case["action"].model_dump() | {"amount_minor": 12000})
    replacement = tasks.propose(changed, case["identity"])
    assert replacement.proposal_id == original.proposal_id
    assert replacement.proposal_version == original.proposal_version + 1
    before = snapshot(case)
    with pytest.raises(TaskError, match="proposal_already_decided"):
        case["approvals"].record(task.task_id, request_for(original, request_id="another-response"), owner_id=task.owner_id)
    with pytest.raises(BusinessError, match="proposal_not_current"):
        case["business"].execute(case["action"], context)
    assert snapshot(case) == before
    fresh = case["approvals"].record(task.task_id, request_for(replacement, request_id="new-version-response"), owner_id=task.owner_id)
    assert fresh.proposal.parameters.amount_minor == 12000
    assert approval.proposal.parameters.amount_minor == 12900
    with pytest.raises(BusinessError, match="amount_mismatch"):
        case["business"].prepare(changed, context_for(case, fresh, operation_key="new-version-operation"))


def test_obsolete_unconfirmed_version_is_refused(case: dict) -> None:
    task = case["task"]
    old = case["tasks"].propose(case["action"], case["identity"])
    current = case["tasks"].propose(case["action"], case["identity"])
    before = snapshot(case)
    with pytest.raises(TaskError, match="proposal_not_current"):
        case["approvals"].record(task.task_id, request_for(old), owner_id=task.owner_id)
    assert snapshot(case) == before
    assert case["approvals"].record(task.task_id, request_for(current), owner_id=task.owner_id).proposal == current


def test_same_proposal_cannot_accept_a_second_request_id(case: dict) -> None:
    task = case["task"]
    proposal = case["tasks"].propose(case["action"], case["identity"])
    case["approvals"].record(task.task_id, request_for(proposal), owner_id=task.owner_id)
    before = snapshot(case)
    with pytest.raises(TaskError, match="proposal_already_decided"):
        case["approvals"].record(task.task_id, request_for(proposal, request_id="different-id"), owner_id=task.owner_id)
    assert snapshot(case) == before


@pytest.mark.parametrize("change", ["owner", "task", "request", "identity"])
def test_confirmation_and_proposals_reject_wrong_binding(case: dict, change: str) -> None:
    task = case["task"]
    proposal = case["tasks"].propose(case["action"], case["identity"])
    request = request_for(proposal)
    before = snapshot(case)
    if change == "owner":
        with pytest.raises(TaskError, match="task_not_found"):
            case["approvals"].record(task.task_id, request, owner_id="other-owner")
    elif change == "task":
        with pytest.raises(TaskError, match="task_not_found"):
            case["approvals"].record("unknown-task", request, owner_id=task.owner_id)
    elif change == "request":
        wrong = ApprovalRequest.model_validate(request.model_dump() | {"proposal_id": "wrong-proposal"})
        with pytest.raises(TaskError, match="proposal_not_current"):
            case["approvals"].record(task.task_id, wrong, owner_id=task.owner_id)
    else:
        wrong = AttemptIdentity.model_validate(case["identity"].model_dump() | {"thread_id": "wrong-thread"})
        with pytest.raises(TaskError, match="attempt_identity_mismatch"):
            case["tasks"].propose(case["action"], wrong)
    assert snapshot(case) == before


def test_request_id_cannot_be_reused_for_another_task(case: dict) -> None:
    task, tasks = case["task"], case["tasks"]
    first = tasks.propose(case["action"], case["identity"])
    case["approvals"].record(task.task_id, request_for(first), owner_id=task.owner_id)
    other = tasks.create(TaskCreate(user_message="another ticket"), owner_id=task.owner_id)
    attempt = tasks.start(other.task_id, owner_id=other.owner_id)
    identity = AttemptIdentity(**{name: getattr(attempt, name) for name in AttemptIdentity.model_fields})
    second = tasks.propose(case["action"], identity)
    before = snapshot(case)
    with pytest.raises(TaskError, match="confirmation_request_conflict"):
        case["approvals"].record(other.task_id, request_for(second), owner_id=other.owner_id)
    assert snapshot(case) == before


def test_expired_proposal_is_refused_at_exact_deadline(case: dict) -> None:
    task = case["task"]
    expires = case["rules"].business_time + timedelta(minutes=1)
    proposal = case["tasks"].propose(case["action"], case["identity"], expires_at=expires)
    service = ApprovalService(case["path"], business_time=expires)
    before = snapshot(case)
    with pytest.raises(TaskError, match="proposal_expired"):
        service.record(task.task_id, request_for(proposal), owner_id=task.owner_id)
    assert snapshot(case) == before


def test_confirmation_cannot_precede_proposal(case: dict) -> None:
    task = case["task"]
    proposal = case["tasks"].propose(case["action"], case["identity"])
    early = ApprovalService(case["path"], business_time=case["rules"].business_time - timedelta(seconds=1))
    before = snapshot(case)
    with pytest.raises(TaskError, match="confirmation_time_mismatch"):
        early.record(task.task_id, request_for(proposal), owner_id=task.owner_id)
    assert snapshot(case) == before


@pytest.mark.parametrize("status", ["cancelled", "completed"])
def test_inactive_task_cannot_be_confirmed(case: dict, status: str) -> None:
    task = case["task"]
    proposal = case["tasks"].propose(case["action"], case["identity"])
    with transaction(case["path"]) as connection:
        TaskRepository(connection).set_status(task.task_id, task.owner_id, TaskStatus(status))
    before = snapshot(case)
    with pytest.raises(TaskError, match="task_not_waiting_approval"):
        case["approvals"].record(task.task_id, request_for(proposal), owner_id=task.owner_id)
    assert snapshot(case) == before


def test_event_failure_rolls_back_confirmation_and_both_statuses(case: dict, monkeypatch) -> None:
    task = case["task"]
    proposal = case["tasks"].propose(case["action"], case["identity"])
    before = snapshot(case)
    original = EventRepository.append

    def fail(self, identity, event_type, *args, **kwargs):
        event = original(self, identity, event_type, *args, **kwargs)
        if event_type == "task_status_changed":
            assert TaskRepository(self.connection).get_task(task.task_id, task.owner_id).status == "running"
            raise RuntimeError("confirmation event failed")
        return event

    monkeypatch.setattr(EventRepository, "append", fail)
    with pytest.raises(RuntimeError, match="confirmation event failed"):
        case["approvals"].record(task.task_id, request_for(proposal), owner_id=task.owner_id)
    assert snapshot(case) == before


def test_proposal_event_failure_rolls_back_version_and_status(case: dict, monkeypatch) -> None:
    before = snapshot(case)

    def fail(*args, **kwargs):
        raise RuntimeError("proposal event failed")

    monkeypatch.setattr(EventRepository, "append", fail)
    with pytest.raises(RuntimeError, match="proposal event failed"):
        case["tasks"].propose(case["action"], case["identity"])
    assert snapshot(case) == before


def test_confirmation_request_cannot_supply_owner_or_parameters(case: dict) -> None:
    proposal = case["tasks"].propose(case["action"], case["identity"])
    with pytest.raises(ValidationError):
        ApprovalRequest.model_validate(request_for(proposal).model_dump() | {
            "owner_id": "model-owner", "parameters": case["action"].model_dump(),
        })
