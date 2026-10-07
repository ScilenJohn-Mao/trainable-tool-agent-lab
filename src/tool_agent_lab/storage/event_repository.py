"""Append per-task events and read owned streams after a sequence cursor."""

import json
import sqlite3
from datetime import datetime

from pydantic import JsonValue

from tool_agent_lab.schemas.events import Event, EventType
from tool_agent_lab.schemas.tasks import AttemptIdentity
from tool_agent_lab.storage.database import require_transaction


class EventRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def append(
        self, identity: AttemptIdentity, event_type: EventType, occurred_at: datetime,
        *, call_id: str | None = None, payload: dict[str, JsonValue] | None = None,
    ) -> Event:
        require_transaction(self.connection)
        row = self.connection.execute(
            """SELECT task_id, owner_id, attempt_id, thread_id, model_version, config_version
                FROM attempts WHERE task_id = ? AND attempt_id = ? AND owner_id = ?""",
            (identity.task_id, identity.attempt_id, identity.owner_id),
        ).fetchone()
        if row is None or any(row[field] != getattr(identity, field) for field in AttemptIdentity.model_fields):
            raise ValueError("Event identity does not match the persisted attempt")
        seq = self.connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE task_id = ?", (identity.task_id,)
        ).fetchone()[0]
        event = Event(
            **dict(row), seq=seq, event_type=event_type, occurred_at=occurred_at,
            call_id=call_id, payload=payload if payload is not None else {},
        )
        values = event.model_dump(mode="json")
        values["payload_json"] = json.dumps(
            values.pop("payload"), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        self.connection.execute(
            """INSERT INTO events (task_id, seq, attempt_id, owner_id, thread_id,
                model_version, config_version, event_type, occurred_at, call_id, payload_json)
            VALUES (:task_id, :seq, :attempt_id, :owner_id, :thread_id,
                :model_version, :config_version, :event_type, :occurred_at, :call_id, :payload_json)""",
            values,
        )
        return event

    def list_events(
        self, task_id: str, owner_id: str, *, after_seq: int = 0, limit: int = 100
    ) -> list[Event]:
        if after_seq < 0 or limit <= 0:
            raise ValueError("after_seq must be nonnegative and limit positive")
        rows = self.connection.execute(
            """SELECT e.* FROM events e JOIN tasks t ON t.task_id = e.task_id
                WHERE e.task_id = ? AND t.owner_id = ? AND e.seq > ? ORDER BY e.seq LIMIT ?""",
            (task_id, owner_id, after_seq, limit),
        )
        events = []
        for row in rows:
            values = dict(row)
            values["payload"] = json.loads(values.pop("payload_json"))
            events.append(Event.model_validate(values))
        return events
