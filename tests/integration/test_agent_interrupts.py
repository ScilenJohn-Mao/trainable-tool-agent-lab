import asyncio
import json

import pytest

from tool_agent_lab.agent.context import history_blocks
from tool_agent_lab.agent.graph import Agent
from tool_agent_lab.agent.inputs import InputRequest
from tool_agent_lab.agent.model_client import AssistantMessage, ModelClient, load_model_config
from tool_agent_lab.runtime.task_service import TaskError
from tool_agent_lab.schemas.actions import ApprovalRequest
from tool_agent_lab.schemas.tasks import TaskCreate
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.checkpoints import open_checkpoints
from tool_agent_lab.storage.database import connect
from tests.integration.test_agent_graph import setup_agent, tool_message


@pytest.mark.parametrize("coupon_decision", ["approved", "rejected"])
def test_reopened_input_and_two_write_interrupts_keep_receipts_thread_and_tool_pairs(tmp_path, coupon_decision):
    transient, tasks, task, rules = setup_agent(tmp_path, [
        AssistantMessage(content='{"kind":"ask_user","question":"Which order?"}')
    ], request=TaskCreate(user_message="My delivery was late and the item was damaged."))
    nodes = transient.nodes
    checkpoints = tmp_path / "checkpoints.sqlite3"
    calls = [tool_message("get_order", {"order_id": "ORD-1002"}, "order-call").tool_calls[0]]
    for name, amount, call_id in (("request_refund", 25900, "refund-call"), ("issue_coupon", 500, "coupon-call")):
        arguments = {"order_id": "ORD-1002", "amount_minor": amount, "reason": "Verified order facts",
                     "policy_refs": [rules.reference(name).model_dump(mode="json")]}
        calls.append(tool_message(name, arguments, call_id).tool_calls[0])

    async def run():
        async with open_checkpoints(checkpoints, application_database=nodes.database) as saver:
            first = Agent(nodes, checkpointer=saver)
            waiting = await first.start(task.task_id, owner_id=task.owner_id)
            thread_id = waiting["identity"]["thread_id"]
            input_target = waiting["waiting"]["request_id"]
        nodes.model = ModelClient(load_model_config(), mock_responses=[
            AssistantMessage(tool_calls=tuple(calls)), AssistantMessage(content="Use the receipts.")])
        async with open_checkpoints(checkpoints, application_database=nodes.database) as saver:
            second = Agent(nodes, checkpointer=saver)
            input_request = InputRequest(request_id="actual-input", input_request_id=input_target, message="ORD-1002")
            refund_wait = await second.submit_input(task.task_id, input_request, owner_id=task.owner_id)
            assert refund_wait["identity"]["thread_id"] == thread_id and refund_wait["clarification_attempted"]
            with pytest.raises(TaskError, match="confirmation_required"):
                await second.resume(task.task_id, "unrecorded-approval", owner_id=task.owner_id)
            proposal = tasks.current_proposal(task.task_id, owner_id=task.owner_id)
            refund_request = ApprovalRequest(request_id="refund-confirm", proposal_id=proposal.proposal_id,
                                             proposal_version=proposal.proposal_version, decision="approved")
            coupon_wait = await second.submit_approval(task.task_id, refund_request, owner_id=task.owner_id)
            assert coupon_wait["__interrupt__"][0].value["proposal"]["parameters"]["action"] == "issue_coupon"
            assert await second.submit_approval(task.task_id, refund_request, owner_id=task.owner_id) == (await second.snapshot(task.task_id, owner_id=task.owner_id)).values
        async with open_checkpoints(checkpoints, application_database=nodes.database) as saver:
            third = Agent(nodes, checkpointer=saver)
            saved = await third.snapshot(task.task_id, owner_id=task.owner_id)
            assert saved.next == ("wait_approval",) and saved.values["identity"]["thread_id"] == thread_id
            proposal = tasks.current_proposal(task.task_id, owner_id=task.owner_id)
            coupon_request = ApprovalRequest(request_id="coupon-confirm", proposal_id=proposal.proposal_id,
                                             proposal_version=proposal.proposal_version, decision=coupon_decision)
            finished = await third.submit_approval(task.task_id, coupon_request, owner_id=task.owner_id)
            assert finished["status"] == "completed" and finished["model_calls"] == 3
            assert finished["identity"]["thread_id"] == thread_id
            assert [m["tool_call_id"] for m in finished["messages"] if m["role"] == "tool"] == ["order-call", "refund-call", "coupon-call"]
            history_blocks(finished["messages"])
            coupon_reply = json.loads(next(m["content"] for m in finished["messages"] if m.get("tool_call_id") == "coupon-call"))
            assert coupon_reply["status"] == ("ok" if coupon_decision == "approved" else "error")
            assert len(finished["operations"]) == (2 if coupon_decision == "approved" else 1)
            assert await third.submit_approval(task.task_id, coupon_request, owner_id=task.owner_id) == finished
        with connect(nodes.database) as connection:
            order = BusinessRepository(connection).get_order("ORD-1002", task.owner_id)
            assert order.refunded_amount_minor == 25900
            assert order.coupon_amount_minor == (500 if coupon_decision == "approved" else 0)

    asyncio.run(run())
