"""Small JSON events; storage assigns per-task sequence numbers."""

import json
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, Field, JsonValue, field_validator, model_validator

from tool_agent_lab.schemas.common import NonEmptyStr, PositiveInt
from tool_agent_lab.schemas.tasks import AttemptIdentity

MAX_EVENT_PAYLOAD_BYTES = 16_384


class EventType(StrEnum):
    TASK_STATUS_CHANGED = "task_status_changed"
    INPUT_REQUESTED = "input_requested"
    INPUT_RECEIVED = "input_received"
    ACTION_PROPOSED = "action_proposed"
    APPROVAL_RECORDED = "approval_recorded"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


class Event(AttemptIdentity):
    seq: PositiveInt
    event_type: EventType
    occurred_at: AwareDatetime
    call_id: NonEmptyStr | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def check_payload_size(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
            raise ValueError(f"event payload exceeds {MAX_EVENT_PAYLOAD_BYTES} UTF-8 bytes")
        return value

    @model_validator(mode="after")
    def check_tool_call_id(self) -> Self:
        if self.event_type in (EventType.TOOL_CALL, EventType.TOOL_RESULT) and self.call_id is None:
            raise ValueError("tool events require call_id")
        return self
