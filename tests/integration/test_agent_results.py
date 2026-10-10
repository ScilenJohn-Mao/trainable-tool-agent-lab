import asyncio
import json

import pytest

from tests.integration.test_agent_graph import setup_agent, tool_message
from tool_agent_lab.agent.model_client import AssistantMessage
from tool_agent_lab.knowledge.citations import CitationError, validate_citation
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


@pytest.mark.parametrize("version", ["mock-policy-v0", "mock-policy-v1", "mock-policy-v2"])
def test_agent_keeps_exact_read_version_without_claiming_historical_applicability(tmp_path, version):
    agent, tasks, task, rules = setup_agent(tmp_path, [
        tool_message("read_policy", {
            "policy_id": "P-DELAY-AMOUNT", "version": version, "section": "amount",
        }, "policy-call"),
        AssistantMessage(content='{"kind":"final","summary":"Policy read."}'),
    ])
    state = asyncio.run(agent.start(task.task_id, owner_id=task.owner_id))
    result = AgentResult.model_validate(state["result"])
    assert result.outcome == "answered" and result.operations == () and result.rule_refs == ()
    assert len(result.policy_citations) == 1
    ref = result.policy_citations[0]
    assert (ref.policy_id, ref.version, ref.section) == ("P-DELAY-AMOUNT", version, "amount")
    text = state["tool_results"][0]["result"]["data"]["text"]
    assert validate_citation(agent.nodes.policy_catalog, ref, excerpt=text).text == text
    if version != "mock-policy-v1":
        with pytest.raises(CitationError, match="citation_not_active"):
            validate_citation(agent.nodes.policy_catalog, ref, business_time=rules.business_time)
    assert tasks.get(task.task_id, owner_id=task.owner_id).status == "completed"


def test_agent_search_and_read_same_section_produce_one_validated_citation(tmp_path):
    agent, _, task, _ = setup_agent(tmp_path, [
        tool_message("search_policy", {
            "query": "延迟补偿券固定500分", "category": "general_goods", "limit": 1,
        }, "search-call"),
        tool_message("read_policy", {
            "policy_id": "P-DELAY-AMOUNT", "version": "mock-policy-v1", "section": "amount",
        }, "read-call"),
        AssistantMessage(content='{"kind":"final","summary":"Evidence collected."}'),
    ])
    state = asyncio.run(agent.start(task.task_id, owner_id=task.owner_id))
    result = AgentResult.model_validate(state["result"])
    assert len(result.policy_citations) == 1
    assert result.policy_citations[0].section == "amount"
    assert [r["tool"] for r in state["tool_results"]] == ["search_policy", "read_policy"]


@pytest.mark.parametrize(("field", "value", "code"), [
    ("section", "missing", "citation_not_found"),
    ("effective_to", "2026-11-01T00:00:00+08:00", "citation_interval_mismatch"),
])
def test_finish_rechecks_saved_citations_before_marking_completed(tmp_path, field, value, code):
    agent, tasks, task, _ = setup_agent(tmp_path, [
        tool_message("read_policy", {
            "policy_id": "P-DELAY-AMOUNT", "version": "mock-policy-v1", "section": "amount",
        }, "read-call"),
        AssistantMessage(content='{"kind":"ask_user","question":"Anything else?"}'),
    ])
    state = asyncio.run(agent.start(task.task_id, owner_id=task.owner_id))
    state["citations"] = [state["citations"][0] | {field: value}]
    with pytest.raises(CitationError, match=code):
        agent.nodes.finish(state)
    assert tasks.get(task.task_id, owner_id=task.owner_id).status == "waiting_input"
