import asyncio
import json

from tests.integration.test_agent_graph import setup_agent, tool_message
from tool_agent_lab.agent.model_client import AssistantMessage
from tool_agent_lab.schemas.actions import ApprovalRequest
from tool_agent_lab.schemas.results import AgentResult
from tool_agent_lab.settings import load_settings


def test_conclusion_uses_actual_amount_ledger_and_retrieved_policy(tmp_path):
    settings = load_settings()
    policy = json.loads((settings.business_data_dir / "policies.json").read_text(encoding="utf-8"))["documents"][0]
    from tool_agent_lab.business.rules import BusinessRules
    rules = BusinessRules.from_file(settings.business_data_dir / "spec.json")
    agent, tasks, task, _ = setup_agent(tmp_path, [
        tool_message("read_policy", {"policy_id": policy["policy_id"], "version": policy["version"]}, "policy-call"),
        tool_message("request_refund", {"order_id": "ORD-1001", "amount_minor": 12900, "reason": "damage",
            "policy_refs": [rules.reference("request_refund").model_dump(mode="json")]}, "refund-call"),
        AssistantMessage(content='{"kind":"final","summary":"I refunded 999999 fen."}'),
    ])

    async def run():
        await agent.start(task.task_id, owner_id=task.owner_id)
        proposal = tasks.current_proposal(task.task_id, owner_id=task.owner_id)
        state = await agent.submit_approval(task.task_id, ApprovalRequest(
            request_id="approve-result", proposal_id=proposal.proposal_id,
            proposal_version=proposal.proposal_version, decision="approved",
        ), owner_id=task.owner_id)
        result = AgentResult.model_validate(state["result"])
        assert result.outcome == "resolved" and result.operations[0].amount_minor == 12900
        assert "12900" in result.summary and "999999" not in result.summary
        assert result.facts["orders"]["ORD-1001"]["refunded_amount_minor"] == 12900
        assert result.policy_citations[0].policy_id == policy["policy_id"]
        assert result.rule_refs == (rules.reference("request_refund"),)
        assert result.operations[0].operation_key in result.summary
        assert "999999" in state["messages"][-1]["content"]

    asyncio.run(run())
