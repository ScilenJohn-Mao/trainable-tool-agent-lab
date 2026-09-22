"""Task identity and persisted attempt metadata; transitions belong to runtime."""

from enum import StrEnum

from pydantic import AwareDatetime

from tool_agent_lab.schemas.common import ContractModel, NonEmptyStr


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskCreate(ContractModel):
    """Untrusted user input; ownership and execution identity are server assigned."""

    user_message: NonEmptyStr
    order_id: NonEmptyStr | None = None


class TaskIdentity(ContractModel):
    task_id: NonEmptyStr
    owner_id: NonEmptyStr


class AttemptIdentity(TaskIdentity):
    attempt_id: NonEmptyStr
    thread_id: NonEmptyStr
    model_version: NonEmptyStr
    config_version: NonEmptyStr


class Task(TaskIdentity):
    user_message: NonEmptyStr
    order_id: NonEmptyStr | None = None
    current_attempt_id: NonEmptyStr | None = None
    status: TaskStatus = TaskStatus.QUEUED
    created_at: AwareDatetime


class Attempt(AttemptIdentity):
    status: TaskStatus = TaskStatus.QUEUED
    created_at: AwareDatetime
