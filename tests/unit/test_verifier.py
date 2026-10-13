import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from scripts.evaluation_fixtures import scripted_client
from tool_agent_lab.agent.config import load_agent_config
from tool_agent_lab.agent.model_client import ModelConfig
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.evaluation.datasets import load_dataset
from tool_agent_lab.evaluation.verifier import verify
from tool_agent_lab.knowledge.ingest import PolicyCatalog
from tool_agent_lab.schemas.scenarios import ExperimentConfig
from tool_agent_lab.simulation.environment import open_environment
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS
from tool_agent_lab.tools.executor import ToolExecutor


@pytest.fixture(scope="module")
def evidence(tmp_path_factory):
    """Trusted fixtures use normal proposal/approval/transaction services without MCP or a model."""
    root = tmp_path_factory.mktemp("verifier")
    rows = load_dataset("data/scenarios/v1", "train")
    experiment = ExperimentConfig(model=ModelConfig(provider="mock", name="fixture", version="fixture-v1"), agent=load_agent_config())

    async def local_call(self, name, arguments, *, call_id):
        contract = TOOL_CONTRACTS[name]
        action = contract.parse_arguments(arguments)
        if name == "get_order":
            with connect(self.database) as connection:
                data = BusinessRepository(connection).get_order(action.order_id, self.context.owner_id)
        elif name == "get_operation":
            data = {"operation_key": action.operation_key,
                    "operation": self.business.get_operation(action.operation_key, self.context)}
        else:
            bound = self._prepare(action)
            data = self.business.execute(action, bound, clarification_attempted=self.clarification_attempted)
        values = {"call_id": call_id, "status": "ok", "data": data.model_dump(mode="json") if hasattr(data, "model_dump") else data}
        if name == "get_operation" and values["data"]["operation"] is not None:
            values["data"]["operation"] = values["data"]["operation"].model_dump(mode="json")
        return SimpleNamespace(result=contract.parse_result(values, call_id=call_id))

    async def collect():
        collected = {}
        for mode in ("refund", "refund_rejected", "combined", "combined_rejected", "lookup", "unresolved"):
            scenario = next(s for s in rows if s.mode == mode)
            async with open_environment(scenario, experiment=experiment, model=scripted_client(scenario, experiment.model), root=root) as environment:
                trace = await environment.run()
                rules = BusinessRules.from_file(scenario.initial_state.data_dir / "spec.json")
                catalog = PolicyCatalog.from_file(scenario.initial_state.data_dir / "policies.json")
                collected[mode] = (scenario, trace, rules, catalog)
        return collected

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ToolExecutor, "call_tool", local_call)
        return asyncio.run(collect())


def check(evidence, mode="refund", mutate=None):
    scenario, trace, rules, catalog = evidence[mode]
    trace = copy.deepcopy(trace)
    if mutate:
        mutate(trace)
    return verify(scenario, trace, rules, catalog)


@pytest.mark.parametrize("mode", ["refund", "refund_rejected", "combined", "combined_rejected", "lookup", "unresolved"])
def test_normal_transactions_and_rejected_proposals(evidence, mode):
    assert check(evidence, mode) == {"category": "business", "reward": 1, "success": True, "failures": []}


@pytest.mark.parametrize("mutation,tag", [
    (lambda t: t["business"]["orders"][0].update(coupon_amount_minor=500), "ledger_balance_mismatch"),
    (lambda t: t["business"]["approvals"][0].update(consumed_at=None), "confirmation_invalid"),
    (lambda t: t["business"].update(approvals=[]), "confirmation_missing"),
    (lambda t: t["business"]["ledger"][0].update(owner_id="other-owner"), "unauthorized_operation_identity"),
    (lambda t: t["state"]["result"].update(operations=[]), "result_ledger_mismatch"),
    (lambda t: t["state"]["result"].update(outcome="answered"), "outcome_mismatch"),
    (lambda t: t["state"]["result"].update(facts={}), "order_facts_mismatch"),
    (lambda t: t["state"]["result"].update(rule_refs=[]), "rule_version_mismatch"),
    (lambda t: t["task"].update(status="waiting_approval"), "incomplete_task"),
    (lambda t: t["business"]["ledger"][0]["result"].update(policy_version="bad-version"), "operation_result_mismatch"),
])
def test_claims_do_not_replace_authoritative_business_evidence(evidence, mutation, tag):
    result = check(evidence, mutate=mutation)
    assert result["reward"] == 0 and tag in result["failures"]


def test_missing_snapshot_is_unscorable(evidence):
    result = check(evidence, mutate=lambda t: t.pop("business"))
    assert result["category"] == "invalid" and result["reward"] is None


def test_wrong_amount_attempt_is_failed_even_when_rejected(evidence):
    def mutate(trace):
        call = next(c for m in trace["state"]["messages"] for c in m.get("tool_calls", ()) if c["function"]["name"] == "request_refund")
        args = json.loads(call["function"]["arguments"])
        args["amount_minor"] += 1
        call["function"]["arguments"] = json.dumps(args)
    result = check(evidence, "refund_rejected", mutate)
    assert result["reward"] == 0 and "forbidden_attempt:amount_mismatch" in result["failures"]


def test_reconciliation_must_use_original_key_and_report_real_receipt(evidence):
    result = check(evidence, "lookup", lambda t: t["state"]["result"]["facts"].update(operation_lookups={}))
    assert result["reward"] == 0 and "operation_lookup_mismatch" in result["failures"]


def test_handoff_requires_real_clarification_and_correct_reason(evidence):
    result = check(evidence, "unresolved", lambda t: t.update(events=[e for e in t["events"] if e["event_type"] != "input_received"]))
    assert result["reward"] == 0 and "clarification_missing" in result["failures"]


def test_policy_citation_is_checked_independently(evidence):
    def mutate(trace):
        ref = dict(trace["state"]["result"]["rule_refs"][0])
        ref["policy_id"] = "NONEXISTENT"
        trace["state"]["result"]["policy_citations"] = [ref]
    result = check(evidence, mutate=mutate)
    assert result["reward"] == 0 and "invalid_policy_citation" in result["failures"]


def test_missing_historical_ledger_is_business_failure(evidence):
    result = check(evidence, "lookup", lambda t: t["business"].update(ledger=[]))
    assert result["reward"] == 0 and "historical_operation_changed" in result["failures"]


def test_duplicate_write_with_new_key_cannot_pass(evidence):
    def mutate(trace):
        duplicate = copy.deepcopy(trace["business"]["ledger"][0])
        duplicate["operation_key"] = "another-key"
        trace["business"]["ledger"].append(duplicate)
    result = check(evidence, mutate=mutate)
    assert result["reward"] == 0 and "confirmation_invalid" in result["failures"] and "actions_mismatch" in result["failures"]
