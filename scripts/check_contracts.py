"""Preview contracts using a synthetic approval; no service or business write runs."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from tool_agent_lab.schemas.actions import (
    ActionProposal,
    Approval,
    ApprovalRequest,
    ExecutionContext,
    PolicyReference,
    RefundAction,
    WriteBinding,
)
from tool_agent_lab.schemas.events import Event
from tool_agent_lab.schemas.tasks import Attempt, Task, TaskCreate
from tool_agent_lab.settings import load_settings


def check_contracts(data_dir: Path) -> dict[str, Any]:
    """Round-trip a read-only example based on the versioned business specification."""
    spec = json.loads((data_dir / "spec.json").read_text(encoding="utf-8"))
    orders = json.loads((data_dir / "orders.json").read_text(encoding="utf-8"))
    order = next(order for order in orders if order["order_id"] == "ORD-1001")
    business_time = datetime.fromisoformat(spec["business_time"])
    identity = {
        "task_id": "contract-preview-task",
        "owner_id": order["owner_id"],
        "attempt_id": "contract-preview-attempt",
        "thread_id": "contract-preview-thread",
        "model_version": "manual",
        "config_version": "contract-preview-v1",
    }
    task_input = TaskCreate(user_message="请为破损的 ORD-1001 退款。", order_id=order["order_id"])
    task = Task(
        **task_input.model_dump(), task_id=identity["task_id"], owner_id=identity["owner_id"],
        current_attempt_id=identity["attempt_id"], status="waiting_approval", created_at=business_time,
    )
    attempt = Attempt(**identity, status="waiting_approval", created_at=business_time)
    proposal = ActionProposal(
        task_id=task.task_id, attempt_id=attempt.attempt_id,
        proposal_id="contract-preview-proposal", proposal_version=1, created_at=business_time,
        parameters=RefundAction(
            order_id=order["order_id"], amount_minor=order["paid_amount_minor"], reason="到货损坏已核实",
            policy_refs=(PolicyReference(
                policy_id=spec["rules"]["refund"]["rule_id"], version=spec["policy_version"],
                section="spec.json#/rules/refund", effective_from=spec["effective_from"],
                effective_to=spec["effective_to"],
            ),),
        ),
    )
    approval_request = ApprovalRequest(
        request_id="contract-preview-request", proposal_id=proposal.proposal_id,
        proposal_version=proposal.proposal_version, decision="approved",
    )
    approval = Approval(
        request=approval_request, proposal=proposal, owner_id=task.owner_id, decided_at=business_time,
    )
    context = ExecutionContext(
        **identity, business_time=business_time,
        write_binding=WriteBinding(
            proposal_id=proposal.proposal_id, proposal_version=proposal.proposal_version,
            approval_request_id=approval_request.request_id, operation_key="contract-preview-refund",
        ),
    )
    event = Event(
        **identity, seq=1, event_type="action_proposed", occurred_at=business_time,
        payload={"proposal_id": proposal.proposal_id, "proposal_version": proposal.proposal_version},
    )
    records = {
        "task_input": task_input, "task": task, "attempt": attempt, "proposal": proposal,
        "approval_request": approval_request, "approval": approval, "context": context, "event": event,
    }
    for record in records.values():
        assert type(record).model_validate_json(record.model_dump_json()) == record
    return {
        "status": "contract_check_passed", "round_trips": len(records),
        "business_execution": "not_run", "approval_service": "not_run",
        "example_kind": "synthetic_contract_snapshots",
        "policy_reference_kind": "business_spec_not_retrieval_document",
        "records": {name: record.model_dump(mode="json") for name, record in records.items()},
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(check_contracts(load_settings().business_data_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
