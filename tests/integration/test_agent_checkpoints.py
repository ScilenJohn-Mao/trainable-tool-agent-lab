import asyncio

from tool_agent_lab.agent.graph import Agent
from tool_agent_lab.agent.model_client import AssistantMessage, ModelClient, load_model_config
from tool_agent_lab.schemas.actions import ApprovalRequest
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.checkpoints import open_checkpoints
from tool_agent_lab.storage.database import connect
from test_agent_graph import setup_agent, tool_message


def test_sqlite_reopen_restores_same_task_thread_and_real_approval(tmp_path):
    transient, tasks, task, rules = setup_agent(tmp_path, [])
    nodes = transient.nodes
    checkpoints = tmp_path / "checkpoints.sqlite3"
    action = {"order_id": "ORD-1001", "amount_minor": 12900, "reason": "Verified damage",
              "policy_refs": [rules.reference("request_refund").model_dump(mode="json")]}

    async def run():
        nodes.model = ModelClient(load_model_config(), mock_responses=[tool_message("request_refund", action, "refund-call")])
        async with open_checkpoints(checkpoints, application_database=nodes.database) as saver:
            first = Agent(nodes, checkpointer=saver)
            paused = await first.start(task.task_id, owner_id=task.owner_id)
            assert paused["__interrupt__"]
            thread_id = paused["identity"]["thread_id"]
        nodes.model = ModelClient(load_model_config(), mock_responses=[AssistantMessage(content="Completed from receipt.")])
        async with open_checkpoints(checkpoints, application_database=nodes.database) as saver:
            second = Agent(nodes, checkpointer=saver)
            saved = await second.snapshot(task.task_id, owner_id=task.owner_id)
            assert saved.next == ("wait_approval",) and saved.values["identity"]["thread_id"] == thread_id
            proposal = tasks.current_proposal(task.task_id, owner_id=task.owner_id)
            request = ApprovalRequest(request_id="confirm-after-reopen", proposal_id=proposal.proposal_id,
                                      proposal_version=proposal.proposal_version, decision="approved")
            finished = await second.submit_approval(task.task_id, request, owner_id=task.owner_id)
            assert finished["status"] == "completed" and finished["model_calls"] == 2
            assert finished["identity"]["thread_id"] == thread_id
            assert await second.submit_approval(task.task_id, request, owner_id=task.owner_id) == finished
        with connect(nodes.database) as connection:
            order = BusinessRepository(connection).get_order("ORD-1001", task.owner_id)
            assert order.refunded_amount_minor == 12900
        async with open_checkpoints(checkpoints, application_database=nodes.database) as saver:
            saved = await Agent(nodes, checkpointer=saver).snapshot(task.task_id, owner_id=task.owner_id)
            assert saved.values["status"] == "completed" and not saved.next

    asyncio.run(run())
