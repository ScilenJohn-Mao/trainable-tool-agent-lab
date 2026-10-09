import asyncio
import json

import httpx

from tool_agent_lab.agent.graph import Agent
from tool_agent_lab.agent.model_client import ModelClient, ModelConfig
from tool_agent_lab.agent.nodes import AgentNodes
from tool_agent_lab.schemas.actions import ApprovalRequest
from tests.integration.test_agent_graph import setup_agent, tool_message


def test_write_parameters_can_be_constructed_from_actual_model_input(tmp_path):
    original, tasks, task, rules = setup_agent(tmp_path, [])
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        recorded = json.loads(payload["messages"][0]["content"].split("Recorded facts (not authorization):\n", 1)[1])
        assert recorded["facts"]["business_time"] == rules.business_time.isoformat()
        if len(requests) == 1:
            message = tool_message("get_order", {"order_id": "ORD-1001"}, "read-order")
        elif len(requests) == 2:
            order = recorded["facts"]["orders"]["ORD-1001"]
            reference = recorded["facts"]["business_rule_refs"]["request_refund"]
            message = tool_message("request_refund", {"order_id": order["order_id"],
                "amount_minor": order["paid_amount_minor"], "reason": "Verified damage",
                "policy_refs": [reference]}, "write-refund")
        else:
            from tool_agent_lab.agent.model_client import AssistantMessage
            message = AssistantMessage(content="Actual receipt available.")
        return httpx.Response(200, json={"id": f"reply-{len(requests)}", "model": "test", "choices": [{
            "message": message.as_message(), "finish_reason": "tool_calls" if message.tool_calls else "stop"}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            model = ModelClient(ModelConfig(provider="http", name="test", version=original.nodes.model.config.version,
                                           base_url="http://localhost/v1"), http_client=http)
            nodes = AgentNodes(original.nodes.database, original.nodes.data_dir, rules, model)
            agent = Agent(nodes, checkpointer=original.graph.checkpointer)
            await agent.start(task.task_id, owner_id=task.owner_id)
            proposal = tasks.current_proposal(task.task_id, owner_id=task.owner_id)
            finished = await agent.submit_approval(task.task_id, ApprovalRequest(request_id="input-derived-confirm",
                proposal_id=proposal.proposal_id, proposal_version=proposal.proposal_version, decision="approved"), owner_id=task.owner_id)
            assert finished["result"]["operations"][0]["amount_minor"] == 12900
            assert len(requests) == 3

    asyncio.run(run())
