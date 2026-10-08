"""Create and inspect owned tasks and record decisions on persisted proposals."""

from typing import Annotated

from fastapi import APIRouter, Query

from apps.api.dependencies import Approvals, Owner, Tasks
from tool_agent_lab.runtime.task_service import TaskError
from tool_agent_lab.schemas.actions import ActionProposal, Approval, ApprovalRequest
from tool_agent_lab.schemas.tasks import Task, TaskCreate, TaskStatus

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", response_model=Task, status_code=201)
def create_task(request: TaskCreate, tasks: Tasks, owner_id: Owner) -> Task:
    return tasks.create(request, owner_id=owner_id)


@router.get("", response_model=list[Task])
def list_tasks(
    tasks: Tasks, owner_id: Owner, status: TaskStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Task]:
    return tasks.list(owner_id=owner_id, status=status, limit=limit, offset=offset)


@router.get("/{task_id}", response_model=Task)
def get_task(task_id: str, tasks: Tasks, owner_id: Owner) -> Task:
    task = tasks.get(task_id, owner_id=owner_id)
    if task is None:
        raise TaskError("task_not_found")
    return task


@router.get("/{task_id}/proposal", response_model=ActionProposal | None)
def get_proposal(task_id: str, tasks: Tasks, owner_id: Owner) -> ActionProposal | None:
    get_task(task_id, tasks, owner_id)
    return tasks.current_proposal(task_id, owner_id=owner_id)


@router.post("/{task_id}/approval", response_model=Approval)
def record_approval(task_id: str, request: ApprovalRequest, approvals: Approvals, owner_id: Owner) -> Approval:
    return approvals.record(task_id, request, owner_id=owner_id)
