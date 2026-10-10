"""Create and inspect owned tasks and record decisions on persisted proposals."""

import json
from typing import Annotated

from fastapi import APIRouter, Query, Request
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from apps.api.dependencies import Approvals, Inputs, Owner, Tasks
from tool_agent_lab.agent.inputs import InputRequest
from tool_agent_lab.runtime.task_service import TaskError, load_current_attempt
from tool_agent_lab.schemas.actions import ActionProposal, Approval, ApprovalRequest
from tool_agent_lab.schemas.common import ContractModel, NonEmptyStr
from tool_agent_lab.schemas.results import AgentResult
from tool_agent_lab.schemas.tasks import Attempt, AttemptIdentity, Task, TaskCreate, TaskStatus
from tool_agent_lab.storage.database import connect
from tool_agent_lab.storage.task_repository import TaskRepository

router = APIRouter(prefix="/tasks", tags=["tasks"])


class InputPrompt(ContractModel):
    kind: str
    request_id: NonEmptyStr
    question: NonEmptyStr


class TaskDetail(Task):
    attempt: Attempt
    input_request: InputPrompt | None = None
    result: AgentResult | None = None


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


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task(task_id: str, request: Request, tasks: Tasks, owner_id: Owner) -> TaskDetail:
    with connect(tasks.database) as connection:
        task, attempt = load_current_attempt(TaskRepository(connection), task_id, owner_id)
        prompt = None
        if task.status == TaskStatus.WAITING_INPUT:
            row = connection.execute(
                """SELECT payload_json FROM events WHERE task_id=? AND attempt_id=? AND owner_id=?
                AND event_type='input_requested' ORDER BY seq DESC LIMIT 1""",
                (task_id, attempt.attempt_id, owner_id),
            ).fetchone()
            prompt = InputPrompt.model_validate(json.loads(row[0])) if row else None
    result = None
    checkpoint = request.app.state.settings.checkpoint_db_path
    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED) and checkpoint.is_file():
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint)) as saver:
            saved = await saver.aget_tuple({"configurable": {"thread_id": attempt.thread_id}})
        if saved:
            values = saved.checkpoint["channel_values"]
            identity = {field: getattr(attempt, field) for field in AttemptIdentity.model_fields}
            if values.get("identity") != identity:
                raise TaskError("checkpoint_identity_mismatch")
            # The graph saves its final result after the application status transaction.
            if values.get("status") == task.status and values.get("result"):
                result = AgentResult.model_validate(values["result"])
    return TaskDetail(**task.model_dump(), attempt=attempt, input_request=prompt, result=result)


@router.get("/{task_id}/proposal", response_model=ActionProposal | None)
def get_proposal(task_id: str, tasks: Tasks, owner_id: Owner) -> ActionProposal | None:
    if tasks.get(task_id, owner_id=owner_id) is None:
        raise TaskError("task_not_found")
    return tasks.current_proposal(task_id, owner_id=owner_id)


@router.post("/{task_id}/approval", response_model=Approval)
def record_approval(task_id: str, request: ApprovalRequest, approvals: Approvals, owner_id: Owner) -> Approval:
    return approvals.record(task_id, request, owner_id=owner_id)


@router.post("/{task_id}/input", response_model=InputRequest)
def record_input(task_id: str, request: InputRequest, inputs: Inputs, owner_id: Owner) -> InputRequest:
    return inputs.record(task_id, request, owner_id=owner_id)


@router.post("/{task_id}/cancel", response_model=Task)
def cancel_task(task_id: str, tasks: Tasks, owner_id: Owner) -> Task:
    return tasks.cancel(task_id, owner_id=owner_id)
