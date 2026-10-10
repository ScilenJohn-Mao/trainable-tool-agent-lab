"""Stream owned persisted task events as basic server-sent feedback."""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from apps.api.dependencies import Owner, Tasks
from tool_agent_lab.runtime.task_service import TaskError, TaskService
from tool_agent_lab.schemas.tasks import TaskStatus
from tool_agent_lab.storage.database import connect
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository

router = APIRouter(prefix="/tasks", tags=["events"])
TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


async def event_stream(request: Request, task_id: str, tasks: TaskService,
                       owner_id: str, after_seq: int) -> AsyncIterator[str]:
    last_heartbeat = time.monotonic()
    while not await request.is_disconnected():
        with connect(tasks.database) as connection:
            # Read status first so a newly committed terminal event is never omitted.
            task = TaskRepository(connection).get_task(task_id, owner_id)
            if task is None:
                return
            events = EventRepository(connection).list_events(task_id, owner_id, after_seq=after_seq, limit=100)
        for event in events:
            yield f"id: {event.seq}\nevent: {event.event_type.value}\ndata: {event.model_dump_json()}\n\n"
            after_seq = event.seq
        if task.status in TERMINAL_STATUSES and len(events) < 100:
            return
        if len(events) == 100:
            continue
        if time.monotonic() - last_heartbeat >= 15:
            yield ": keep-alive\n\n"
            last_heartbeat = time.monotonic()
        await asyncio.sleep(0.5)


@router.get("/{task_id}/events", response_class=StreamingResponse,
            responses={200: {"content": {"text/event-stream": {}}}})
async def stream_events(task_id: str, request: Request, tasks: Tasks, owner_id: Owner,
                        after_seq: Annotated[int, Query(ge=0)] = 0) -> StreamingResponse:
    if tasks.get(task_id, owner_id=owner_id) is None:
        raise TaskError("task_not_found")
    return StreamingResponse(event_stream(request, task_id, tasks, owner_id, after_seq),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
