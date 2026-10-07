"""Record human decisions against the current persisted proposal snapshot."""

from datetime import datetime
from pathlib import Path

from pydantic import AwareDatetime, TypeAdapter

from tool_agent_lab.runtime.task_service import TaskError, change_status, load_current_attempt
from tool_agent_lab.schemas.actions import Approval, ApprovalRequest
from tool_agent_lab.schemas.events import EventType
from tool_agent_lab.schemas.tasks import TaskStatus
from tool_agent_lab.storage.database import transaction
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository


class ApprovalService:
    def __init__(self, database: str | Path, *, business_time: datetime) -> None:
        self.database = Path(database)
        self.business_time = TypeAdapter(AwareDatetime).validate_python(business_time)

    def record(self, task_id: str, request: ApprovalRequest, *, owner_id: str) -> Approval:
        """Use server-resolved ownership; a repeated exact request returns its original receipt."""
        with transaction(self.database) as connection:
            tasks = TaskRepository(connection)
            task, attempt = load_current_attempt(tasks, task_id, owner_id)
            existing = tasks.get_approval(task_id, request.request_id, owner_id)
            if existing is not None:
                if existing.request != request:
                    raise TaskError("confirmation_request_conflict")
                return existing
            if tasks.approval_request_exists(request.request_id):
                raise TaskError("confirmation_request_conflict")
            if tasks.get_proposal_approval(task_id, request.proposal_id, request.proposal_version, owner_id) is not None:
                raise TaskError("proposal_already_decided")
            if task.status != TaskStatus.WAITING_APPROVAL or attempt.status != TaskStatus.WAITING_APPROVAL:
                raise TaskError("task_not_waiting_approval")
            proposal = tasks.get_current_proposal(task_id, attempt.attempt_id, owner_id)
            if proposal is None or (request.proposal_id, request.proposal_version) != (
                proposal.proposal_id, proposal.proposal_version,
            ):
                raise TaskError("proposal_not_current")
            if proposal.expires_at is not None and self.business_time >= proposal.expires_at:
                raise TaskError("proposal_expired")
            if self.business_time < proposal.created_at:
                raise TaskError("confirmation_time_mismatch")
            approval = Approval(request=request, proposal=proposal, owner_id=task.owner_id, decided_at=self.business_time)
            tasks.add_approval(approval)
            events = EventRepository(connection)
            events.append(attempt, EventType.APPROVAL_RECORDED, self.business_time, payload={
                "request_id": request.request_id, "proposal_id": proposal.proposal_id,
                "proposal_version": proposal.proposal_version, "decision": request.decision.value,
            })
            change_status(tasks, events, attempt, TaskStatus.RUNNING, self.business_time)
            return approval
