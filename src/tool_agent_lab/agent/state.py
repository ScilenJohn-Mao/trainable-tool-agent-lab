"""JSON-compatible graph state bound to a persisted task attempt."""

from pathlib import Path
from typing import Any, TypedDict

from tool_agent_lab.runtime.task_service import TaskError, load_current_attempt
from tool_agent_lab.schemas.tasks import AttemptIdentity
from tool_agent_lab.storage.database import connect
from tool_agent_lab.storage.task_repository import TaskRepository


class AgentState(TypedDict):
    identity: dict[str, str]
    messages: list[dict[str, Any]]
    model_calls: int
    pending_calls: list[dict[str, Any]]
    active_call: dict[str, Any] | None
    proposal: dict[str, Any] | None
    waiting: dict[str, Any] | None
    clarification_attempted: bool
    tool_results: list[dict[str, Any]]
    operations: list[dict[str, Any]]
    facts: dict[str, Any]
    citations: list[dict[str, Any]]
    result: dict[str, Any] | None
    status: str


def identity_of(state: AgentState) -> AttemptIdentity:
    return AttemptIdentity.model_validate(state["identity"])


def load_agent_state(
    database: str | Path, task_id: str, *, owner_id: str, model_version: str,
) -> AgentState:
    """Resolve identity from storage; model output cannot select another attempt."""
    with connect(database) as connection:
        task, attempt = load_current_attempt(TaskRepository(connection), task_id, owner_id)
    if attempt.model_version != model_version:
        raise TaskError("model_version_mismatch")
    identity = {field: getattr(attempt, field) for field in AttemptIdentity.model_fields}
    return AgentState(
        identity=identity, messages=[{"role": "user", "content": task.user_message}],
        model_calls=0, pending_calls=[], active_call=None, proposal=None, waiting=None,
        clarification_attempted=False, tool_results=[], operations=[], citations=[], result=None,
        facts={"order_id": task.order_id} if task.order_id else {}, status=task.status.value,
    )
