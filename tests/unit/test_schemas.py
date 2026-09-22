"""Contract checks only: no database, approval service or business execution."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.check_contracts import check_contracts
from tool_agent_lab.schemas.actions import (
    ActionProposal,
    Approval,
    ApprovalRequest,
    ExecutionContext,
    HandoffAction,
    PolicyReference,
    RefundAction,
)
from tool_agent_lab.schemas.events import Event
from tool_agent_lab.schemas.tasks import Task, TaskCreate, TaskStatus
from tool_agent_lab.settings import PROJECT_ROOT


@pytest.fixture
def records() -> dict:
    return check_contracts(PROJECT_ROOT / "data/business/v1")["records"]


def test_refund_snapshots_keep_identity_money_evidence_and_business_time(records: dict) -> None:
    approval = Approval.model_validate_json(json.dumps(records["approval"]))
    context = ExecutionContext.model_validate_json(json.dumps(records["context"]))
    action = approval.proposal.parameters
    assert (action.order_id, action.amount_minor, action.currency) == ("ORD-1001", 12900, "CNY")
    assert action.policy_refs[0].version == "mock-policy-v1"
    assert action.policy_refs[0].section == "spec.json#/rules/refund"
    assert context.business_time.isoformat() == "2026-09-17T12:00:00+08:00"
    assert context.owner_id == approval.owner_id == "demo-user"
    assert context.task_id == approval.proposal.task_id
    assert context.attempt_id == approval.proposal.attempt_id
    assert context.write_binding.approval_request_id == approval.request.request_id
    assert context.write_binding.operation_key == "contract-preview-refund"
    assert (context.model_version, context.config_version) == ("manual", "contract-preview-v1")
    assert context.thread_id == records["attempt"]["thread_id"]


def test_task_states_support_waits_and_terminal_outcomes(records: dict) -> None:
    for status in (
        "queued", "running", "waiting_input", "waiting_approval", "completed", "failed", "cancelled"
    ):
        task = Task.model_validate(records["task"] | {"status": status})
        assert isinstance(task.status, TaskStatus)
        assert Task.model_validate_json(task.model_dump_json()) == task
    with pytest.raises(ValidationError):
        Task.model_validate(records["task"] | {"status": "success"})


@pytest.mark.parametrize("amount", [129.0, True, "12900", 0, -1])
def test_money_cannot_be_coerced_or_nonpositive(records: dict, amount: object) -> None:
    parameters = records["proposal"]["parameters"] | {"amount_minor": amount}
    with pytest.raises(ValidationError, match="amount_minor"):
        ActionProposal.model_validate_json(json.dumps(records["proposal"] | {"parameters": parameters}))


def test_coupon_and_handoff_use_distinct_parameter_shapes(records: dict) -> None:
    refund = records["proposal"]["parameters"]
    coupon = refund | {"action": "issue_coupon", "amount_minor": 500}
    handoff = {
        "action": "create_handoff", "order_id": "ORD-1004", "reason": "unsupported_category",
        "summary": "课程兑换码无法使用", "policy_refs": [refund["policy_refs"][0] | {
            "policy_id": "R-HANDOFF-01", "section": "spec.json#/rules/handoff",
        }],
    }
    for parameters in (coupon, handoff, handoff | {"order_id": None, "reason": "order_unresolved"}):
        proposal = ActionProposal.model_validate(records["proposal"] | {"parameters": parameters})
        assert ActionProposal.model_validate_json(proposal.model_dump_json()) == proposal
    with pytest.raises(ValidationError, match="amount_minor"):
        HandoffAction.model_validate(handoff | {"amount_minor": 500})
    with pytest.raises(ValidationError, match="order_unresolved"):
        HandoffAction.model_validate(handoff | {"order_id": None})
    with pytest.raises(ValidationError, match="order_id"):
        RefundAction.model_validate(refund | {"order_id": None})


def test_user_and_model_parameters_cannot_carry_runtime_authority(records: dict) -> None:
    with pytest.raises(ValidationError, match="owner_id"):
        TaskCreate.model_validate(records["task_input"] | {"owner_id": "another-user"})
    with pytest.raises(ValidationError, match="owner_id"):
        ApprovalRequest.model_validate(records["approval_request"] | {"owner_id": "demo-user"})
    with pytest.raises(ValidationError, match="parameters"):
        ApprovalRequest.model_validate(records["approval_request"] | {
            "parameters": records["proposal"]["parameters"],
        })
    for field in ("operation_key", "execution_context", "approved"):
        with pytest.raises(ValidationError, match=field):
            RefundAction.model_validate(records["proposal"]["parameters"] | {field: "injected"})


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_approval_keeps_the_exact_versioned_snapshot(records: dict, decision: str) -> None:
    request = records["approval_request"] | {"decision": decision}
    approval = Approval.model_validate(records["approval"] | {"request": request})
    assert Approval.model_validate_json(approval.model_dump_json()) == approval
    records["proposal"]["parameters"]["amount_minor"] = 100
    records["proposal"]["proposal_version"] = 2
    changed = ActionProposal.model_validate(records["proposal"])
    assert approval.proposal != changed
    assert approval.proposal.parameters.amount_minor == 12900
    with pytest.raises(ValidationError, match="id and version"):
        Approval.model_validate(records["approval"] | {"request": request, "proposal": changed})
    with pytest.raises(ValidationError, match="frozen"):
        approval.proposal.parameters.amount_minor = 100


def test_approval_cannot_bind_another_proposal(records: dict) -> None:
    request = records["approval_request"] | {"proposal_id": "another-proposal"}
    with pytest.raises(ValidationError, match="id and version"):
        Approval.model_validate(records["approval"] | {"request": request})


def test_read_context_is_valid_but_partial_write_binding_is_not(records: dict) -> None:
    context = records["context"] | {"write_binding": None}
    assert ExecutionContext.model_validate(context).write_binding is None
    with pytest.raises(ValidationError, match="approval_request_id"):
        ExecutionContext.model_validate(context | {"write_binding": {
            "proposal_id": "p1", "proposal_version": 1, "operation_key": "op1",
        }})


@pytest.mark.parametrize("model,key,field", [
    (ActionProposal, "proposal", "created_at"),
    (Approval, "approval", "decided_at"),
    (ExecutionContext, "context", "business_time"),
    (Event, "event", "occurred_at"),
])
def test_business_and_record_timestamps_require_timezones(records: dict, model, key, field) -> None:
    with pytest.raises(ValidationError, match="timezone"):
        model.model_validate(records[key] | {field: "2026-09-17T12:00:00"})


def test_policy_and_proposal_intervals_have_ordered_endpoints(records: dict) -> None:
    reference = records["proposal"]["parameters"]["policy_refs"][0]
    with pytest.raises(ValidationError, match="effective_to"):
        PolicyReference.model_validate(reference | {"effective_to": reference["effective_from"]})
    with pytest.raises(ValidationError, match="expires_at"):
        ActionProposal.model_validate(records["proposal"] | {"expires_at": records["proposal"]["created_at"]})


@pytest.mark.parametrize("event_type", ["tool_call", "tool_result"])
def test_tool_events_can_be_correlated_after_json_round_trip(records: dict, event_type: str) -> None:
    data = records["event"] | {"event_type": event_type}
    with pytest.raises(ValidationError, match="call_id"):
        Event.model_validate(data)
    event = Event.model_validate(data | {"call_id": "call-refund-1"})
    assert Event.model_validate_json(event.model_dump_json()).call_id == "call-refund-1"
    for invalid_seq in (0, True, 1.5):
        with pytest.raises(ValidationError, match="seq"):
            Event.model_validate(data | {"call_id": "call-refund-1", "seq": invalid_seq})


def test_event_payload_has_a_json_byte_budget(records: dict) -> None:
    event = Event.model_validate(records["event"] | {"payload": {"text": "x" * 16_373}})
    assert Event.model_validate_json(event.model_dump_json()) == event
    for text in ("x" * 16_374, "中" * 5_458):
        with pytest.raises(ValidationError, match="16384 UTF-8 bytes"):
            Event.model_validate(records["event"] | {"payload": {"text": text}})
    with pytest.raises(ValidationError):
        Event.model_validate(records["event"] | {"payload": {"value": float("nan")}})


def test_exported_json_schema_separates_action_variants() -> None:
    schema = json.loads(json.dumps(ActionProposal.model_json_schema()))
    discriminator = schema["properties"]["parameters"]["discriminator"]
    assert discriminator["propertyName"] == "action"
    assert set(discriminator["mapping"]) == {"request_refund", "issue_coupon", "create_handoff"}


def test_cli_is_read_only_and_works_from_another_directory(tmp_path: Path) -> None:
    data_dir = PROJECT_ROOT / "data/business/v1"
    before = {path.name: path.read_bytes() for path in data_dir.iterdir()}
    runtime = tmp_path / "runtime"
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts/check_contracts.py")],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=True,
        env={**os.environ, "TTAL_RUNTIME_DIR": str(runtime), "TTAL_BUSINESS_DATA_DIR": str(data_dir)},
    )
    report = json.loads(result.stdout)
    assert report["status"] == "contract_check_passed"
    assert report["round_trips"] == 8
    assert report["records"]["proposal"]["parameters"]["reason"] == "到货损坏已核实"
    assert report["business_execution"] == report["approval_service"] == "not_run"
    assert before == {path.name: path.read_bytes() for path in data_dir.iterdir()}
    assert not runtime.exists()
