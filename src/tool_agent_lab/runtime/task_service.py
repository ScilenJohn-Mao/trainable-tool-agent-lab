"""Create owned tasks and publish versioned action proposals."""

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pydantic import AwareDatetime, TypeAdapter

from tool_agent_lab.schemas.actions import ActionParameters, ActionProposal
from tool_agent_lab.schemas.events import EventType
from tool_agent_lab.schemas.tasks import Attempt, AttemptIdentity, Task, TaskCreate, TaskStatus
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect, transaction
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository


class TaskError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def load_current_attempt(tasks: TaskRepository, task_id: str, owner_id: str) -> tuple[Task, Attempt]:
    task = tasks.get_task(task_id, owner_id)
    if task is None:
        raise TaskError("task_not_found")
    if task.current_attempt_id is None:
        raise TaskError("attempt_not_current")
    attempt = tasks.get_attempt(task_id, task.current_attempt_id, owner_id)
    if attempt is None:
        raise TaskError("attempt_not_current")
    return task, attempt


def change_status(
    tasks: TaskRepository, events: EventRepository, attempt: Attempt,
    status: TaskStatus, business_time: datetime,
) -> None:
    """Update task and attempt together inside the caller's transaction."""
    tasks.set_status(attempt.task_id, attempt.owner_id, status)
    tasks.set_attempt_status(attempt.task_id, attempt.attempt_id, attempt.owner_id, status)
    events.append(attempt, EventType.TASK_STATUS_CHANGED, business_time, payload={"status": status.value})


class TaskService:
    def __init__(
        self, database: str | Path, *, business_time: datetime,
        model_version: str = "manual", config_version: str = "app-v1",
    ) -> None:
        self.database = Path(database)
        self.business_time = TypeAdapter(AwareDatetime).validate_python(business_time)
        self.model_version = model_version
        self.config_version = config_version

    def create(self, request: TaskCreate, *, owner_id: str) -> Task:
        """Ownership is supplied by the application, never by the user request schema."""
        task_id, attempt_id = f"task-{uuid4().hex}", f"attempt-{uuid4().hex}"
        task = Task(
            **request.model_dump(), task_id=task_id, owner_id=owner_id,
            current_attempt_id=attempt_id, created_at=self.business_time,
        )
        attempt = Attempt(
            task_id=task_id, owner_id=task.owner_id, attempt_id=attempt_id,
            thread_id=f"thread-{uuid4().hex}", model_version=self.model_version,
            config_version=self.config_version, created_at=self.business_time,
        )
        with transaction(self.database) as connection:
            if request.order_id is not None and BusinessRepository(connection).get_order(request.order_id, task.owner_id) is None:
                raise TaskError("order_not_found")
            tasks = TaskRepository(connection)
            tasks.add_task(task)
            tasks.add_attempt(attempt)
            EventRepository(connection).append(
                attempt, EventType.TASK_STATUS_CHANGED, self.business_time, payload={"status": "queued"},
            )
        return task

    def get(self, task_id: str, *, owner_id: str) -> Task | None:
        with connect(self.database) as connection:
            return TaskRepository(connection).get_task(task_id, owner_id)

    def list(
        self, *, owner_id: str, status: TaskStatus | None = None, limit: int = 50, offset: int = 0,
    ) -> list[Task]:
        with connect(self.database) as connection:
            return TaskRepository(connection).list_tasks(owner_id, status=status, limit=limit, offset=offset)

    def start(self, task_id: str, *, owner_id: str) -> Attempt:
        with transaction(self.database) as connection:
            tasks = TaskRepository(connection)
            task, attempt = load_current_attempt(tasks, task_id, owner_id)
            if task.status == attempt.status == TaskStatus.RUNNING:
                return attempt
            if task.status != TaskStatus.QUEUED or attempt.status != TaskStatus.QUEUED:
                raise TaskError("task_not_queued")
            change_status(tasks, EventRepository(connection), attempt, TaskStatus.RUNNING, self.business_time)
            return tasks.get_attempt(task_id, attempt.attempt_id, owner_id)

    def propose(
        self, action: ActionParameters, identity: AttemptIdentity, *, expires_at: datetime | None = None,
    ) -> ActionProposal:
        with transaction(self.database) as connection:
            tasks = TaskRepository(connection)
            task, attempt = load_current_attempt(tasks, identity.task_id, identity.owner_id)
            if any(getattr(attempt, name) != getattr(identity, name) for name in AttemptIdentity.model_fields):
                raise TaskError("attempt_identity_mismatch")
            if task.status not in (TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL) or task.status != attempt.status:
                raise TaskError("task_not_executable")
            if action.order_id is not None and BusinessRepository(connection).get_order(action.order_id, identity.owner_id) is None:
                raise TaskError("order_not_found")
            previous = tasks.get_current_proposal(identity.task_id, identity.attempt_id, identity.owner_id)
            proposal = ActionProposal(
                task_id=identity.task_id, attempt_id=identity.attempt_id,
                proposal_id=previous.proposal_id if previous else f"proposal-{uuid4().hex}",
                proposal_version=previous.proposal_version + 1 if previous else 1,
                parameters=action, created_at=self.business_time, expires_at=expires_at,
            )
            tasks.add_proposal(proposal, identity.owner_id)
            events = EventRepository(connection)
            events.append(attempt, EventType.ACTION_PROPOSED, self.business_time, payload={
                "proposal_id": proposal.proposal_id, "proposal_version": proposal.proposal_version,
            })
            change_status(tasks, events, attempt, TaskStatus.WAITING_APPROVAL, self.business_time)
            return proposal

    def current_proposal(self, task_id: str, *, owner_id: str) -> ActionProposal | None:
        with connect(self.database) as connection:
            tasks = TaskRepository(connection)
            task = tasks.get_task(task_id, owner_id)
            if task is None or task.current_attempt_id is None:
                return None
            return tasks.get_current_proposal(task_id, task.current_attempt_id, owner_id)
