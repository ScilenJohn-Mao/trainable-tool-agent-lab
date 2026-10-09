import asyncio

import pytest

from tests.integration.test_agent_graph import setup_agent, tool_message
from tool_agent_lab.agent.inputs import InputRequest
from tool_agent_lab.agent.model_client import AssistantMessage
from tool_agent_lab.runtime.task_service import TaskError
from tool_agent_lab.schemas.tasks import TaskCreate
from tool_agent_lab.storage.database import connect
from tool_agent_lab.storage.event_repository import EventRepository


def test_real_input_is_recorded_bound_and_resumed_once(tmp_path):
    agent, tasks, task, _ = setup_agent(tmp_path, [
        AssistantMessage(content='{"kind":"ask_user","question":"Which order?"}'),
        tool_message("get_order", {"order_id": "ORD-1001"}, "order-call"),
        AssistantMessage(content='{"kind":"final","summary":"Order found."}'),
    ], request=TaskCreate(user_message="My product is damaged"))

    async def run():
        waiting = await agent.start(task.task_id, owner_id=task.owner_id)
        assert tasks.get(task.task_id, owner_id=task.owner_id).status == "waiting_input"
        request_id = waiting["waiting"]["request_id"]
        with pytest.raises(TaskError, match="input_receipt_required"):
            await agent.resume(task.task_id, "unrecorded-input", owner_id=task.owner_id)
        with pytest.raises(TaskError, match="input_request_not_current"):
            await agent.submit_input(task.task_id, InputRequest(request_id="reply", input_request_id="old", message="ORD-1001"), owner_id=task.owner_id)
        request = InputRequest(request_id="reply", input_request_id=request_id, message="ORD-1001")
        final = await agent.submit_input(task.task_id, request, owner_id=task.owner_id)
        assert final["clarification_attempted"] and final["status"] == "completed"
        assert final["input_receipts"] == [request.model_dump(mode="json")]
        assert (await agent.submit_input(task.task_id, request, owner_id=task.owner_id)) == final
        assert sum(m["role"] == "user" and m["content"] == "ORD-1001" for m in final["messages"]) == 1
        with connect(agent.nodes.database) as connection:
            events = EventRepository(connection).list_events(task.task_id, task.owner_id)
        assert sum(e.event_type == "input_received" for e in events) == 1

    asyncio.run(run())
