"""Record owned supplemental input before resuming a graph interrupt."""

import json
from pathlib import Path

from pydantic import Field

from tool_agent_lab.runtime.task_service import TaskError, change_status, load_current_attempt
from tool_agent_lab.schemas.common import ContractModel, NonEmptyStr
from tool_agent_lab.schemas.tasks import TaskStatus
from tool_agent_lab.storage.database import connect, transaction
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository


class InputRequest(ContractModel):
    request_id: NonEmptyStr
    input_request_id: NonEmptyStr
    message: NonEmptyStr = Field(max_length=4000)


class InputService:
    def __init__(self, database: str | Path, *, business_time) -> None:
        self.database = Path(database)
        self.business_time = business_time

    def get(self, task_id: str, request_id: str, *, owner_id: str) -> InputRequest | None:
        with connect(self.database) as connection:
            _, attempt = load_current_attempt(TaskRepository(connection), task_id, owner_id)
            row = connection.execute(
                """SELECT payload_json FROM events WHERE task_id=? AND attempt_id=? AND owner_id=?
                AND event_type='input_received' AND json_extract(payload_json,'$.request_id')=?""",
                (task_id, attempt.attempt_id, owner_id, request_id),
            ).fetchone()
            return InputRequest.model_validate_json(row[0]) if row else None

    def record(self, task_id: str, request: InputRequest, *, owner_id: str) -> InputRequest:
        with transaction(self.database) as connection:
            tasks = TaskRepository(connection)
            task, attempt = load_current_attempt(tasks, task_id, owner_id)
            row = connection.execute(
                """SELECT payload_json FROM events WHERE task_id=? AND attempt_id=? AND owner_id=?
                AND event_type='input_received' AND json_extract(payload_json,'$.request_id')=?""",
                (task_id, attempt.attempt_id, owner_id, request.request_id),
            ).fetchone()
            if row:
                stored = InputRequest.model_validate_json(row[0])
                if stored != request:
                    raise TaskError("input_request_conflict")
                return stored
            if task.status != attempt.status or task.status != TaskStatus.WAITING_INPUT:
                raise TaskError("task_not_waiting_input")
            row = connection.execute(
                """SELECT payload_json FROM events WHERE task_id=? AND attempt_id=? AND owner_id=?
                AND event_type='input_requested' ORDER BY seq DESC LIMIT 1""",
                (task_id, attempt.attempt_id, owner_id),
            ).fetchone()
            if row is None or json.loads(row[0])["request_id"] != request.input_request_id:
                raise TaskError("input_request_not_current")
            events = EventRepository(connection)
            events.append(attempt, "input_received", self.business_time, payload=request.model_dump(mode="json"))
            change_status(tasks, events, attempt, TaskStatus.RUNNING, self.business_time)
            return request
