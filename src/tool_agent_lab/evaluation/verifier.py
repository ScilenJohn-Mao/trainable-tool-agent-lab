"""Binary business verification from database snapshots, independent of model claims."""

from collections import Counter
import json

from pydantic import ValidationError

from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.knowledge.citations import validate_citation
from tool_agent_lab.knowledge.ingest import PolicyCatalog
from tool_agent_lab.schemas.actions import Approval
from tool_agent_lab.schemas.business import Operation, Order
from tool_agent_lab.schemas.results import AgentResult
from tool_agent_lab.schemas.scenarios import Scenario
from tool_agent_lab.schemas.tasks import AttemptIdentity, Task
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS


def verdict(category: str, failures=()) -> dict:
    failures = sorted(set(failures))
    reward = None if category in ("infrastructure", "invalid") else int(not failures)
    return {"category": category, "reward": reward, "success": reward == 1, "failures": failures}


def verify(scenario: Scenario, trajectory: dict, rules: BusinessRules, catalog: PolicyCatalog) -> dict:
    """Malformed evidence is unscorable; observed business/model failures receive zero."""
    try:
        return _verify(scenario, trajectory, rules, catalog)
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        return verdict("invalid", ["invalid_evidence:" + type(error).__name__])


def _verify(scenario, trace, rules, catalog):
    if trace["error"] is not None:
        return verdict("invalid", ["execution_error_requires_classification"])
    task = Task.model_validate(trace["task"])
    state = trace["state"]
    identity = AttemptIdentity.model_validate(state["identity"])
    if (task.task_id, task.current_attempt_id, task.owner_id) != (identity.task_id, identity.attempt_id, identity.owner_id):
        return verdict("invalid", ["identity_mismatch"])
    if identity.owner_id != scenario.owner_id or trace["scenario"]["scenario_id"] != scenario.scenario_id:
        return verdict("invalid", ["scenario_mismatch"])
    before = {o.order_id: o for o in map(Order.model_validate, trace["initial_business"]["orders"])}
    after = {o.order_id: o for o in map(Order.model_validate, trace["business"]["orders"])}
    initial = {o.operation_key: o for o in map(Operation.model_validate, trace["initial_business"]["ledger"])}
    ledger_list = list(map(Operation.model_validate, trace["business"]["ledger"]))
    ledger = {o.operation_key: o for o in ledger_list}
    if len(ledger) != len(ledger_list):
        return verdict("invalid", ["duplicate_ledger_key"])
    approvals = {}
    for record in trace["business"]["approvals"]:
        approval = Approval.model_validate({k: record[k] for k in Approval.model_fields})
        key = approval.request.request_id
        if key in approvals:
            return verdict("invalid", ["duplicate_confirmation"])
        approvals[key] = (approval, record["consumed_at"])
    if scenario.initial_state is not None and before != {o.order_id: o for o in scenario.initial_state.orders}:
        return verdict("invalid", ["initial_state_mismatch"])
    failures = []
    events = trace["events"]
    clarified = any(e["event_type"] == "input_received" for e in events)
    if set(before) != set(after):
        failures.append("order_set_changed")
    if any(ledger.get(key) != op for key, op in initial.items()):
        failures.append("historical_operation_changed")
    new = [op for key, op in ledger.items() if key not in initial]
    committed = [op for op in new if op.status == "succeeded"]
    if any((op.task_id, op.attempt_id, op.owner_id) != (identity.task_id, identity.attempt_id, identity.owner_id) for op in new):
        failures.append("unauthorized_operation_identity")
    if any(op.status != "succeeded" for op in new):
        failures.append("unresolved_operation")
    used = set()
    balances = dict(before)
    for op in sorted(committed, key=lambda o: (o.committed_at, o.operation_key)):
        stored = approvals.get(op.approval_request_id)
        if stored is None:
            failures.append("confirmation_missing")
            continue
        approval, consumed = stored
        proposal, action = approval.proposal, approval.proposal.parameters
        if (approval.owner_id != op.owner_id or proposal.task_id != op.task_id or proposal.attempt_id != op.attempt_id
                or approval.request.decision != "approved" or op.approval_request_id in used
                or consumed != op.committed_at.isoformat()
                or not proposal.created_at <= approval.decided_at <= op.committed_at
                or (proposal.expires_at is not None and op.committed_at >= proposal.expires_at)):
            failures.append("confirmation_invalid")
        used.add(op.approval_request_id)
        if (action.action, action.order_id, getattr(action, "amount_minor", None)) != (op.action, op.order_id, op.amount_minor):
            failures.append("confirmation_parameters_mismatch")
        decision = rules.evaluate(action, balances.get(op.order_id), business_time=op.committed_at,
                                  clarification_attempted=clarified)
        expected_receipt = {"operation_key": op.operation_key, "action": op.action, "order_id": op.order_id,
                            "amount_minor": op.amount_minor, "currency": op.currency, "rule_id": decision.rule_id,
                            "policy_version": decision.policy_version, "reason": action.reason}
        if not isinstance(op.result, dict) or any(op.result.get(k) != value for k, value in expected_receipt.items()):
            failures.append("operation_result_mismatch")
        if not decision.allowed:
            failures.append("illegal_commit:" + decision.code.value)
        if op.action == "create_handoff" and action.reason != scenario.expected.handoff_reason:
            failures.append("handoff_reason_mismatch")
        if op.order_id in balances and op.action in ("request_refund", "issue_coupon"):
            field = "refunded_amount_minor" if op.action == "request_refund" else "coupon_amount_minor"
            order = balances[op.order_id]
            balances[op.order_id] = order.model_copy(update={field: getattr(order, field) + op.amount_minor})
    if balances != after:
        failures.append("ledger_balance_mismatch")
    expected = scenario.expected
    if Counter(op.action for op in committed) != Counter(expected.new_actions):
        failures.append("actions_mismatch")
    if any(op.order_id != expected.order_id for op in committed):
        failures.append("operation_order_mismatch")
    order = after.get(expected.order_id)
    if (order.refunded_amount_minor if order else 0, order.coupon_amount_minor if order else 0) != (
            expected.refund_total_minor, expected.coupon_total_minor):
        failures.append("amounts_mismatch")
    if any((e["task_id"], e["attempt_id"], e["owner_id"]) != (identity.task_id, identity.attempt_id, identity.owner_id) for e in events):
        return verdict("invalid", ["event_identity_mismatch"])
    if expected.must_clarify and not any(e["event_type"] == "input_received" for e in events):
        failures.append("clarification_missing")
    decisions = [e["payload"]["decision"] for e in events if e["event_type"] == "approval_recorded"]
    if decisions != list(scenario.user.approval_decisions):
        failures.append("confirmation_sequence_mismatch")
    # Replay only observed model/tool messages for attempted actions, never generate decisions from labels.
    observed_orders = dict(before)
    clarification_seen = False
    user_messages = 0
    for message in state["messages"]:
        if message["role"] == "user":
            user_messages += 1
            if user_messages > 1 and clarified and any(r["message"] == message["content"] for r in state["input_receipts"]):
                clarification_seen = True
        elif message["role"] == "assistant":
            for call in message.get("tool_calls", ()):
                name = call["function"]["name"]
                contract = TOOL_CONTRACTS.get(name)
                if contract is None:
                    failures.append("unknown_tool")
                    continue
                try:
                    action = contract.parse_arguments(json.loads(call["function"]["arguments"]))
                except (ValueError, ValidationError):
                    failures.append("invalid_arguments")
                    continue
                if not contract.read_only:
                    decision = rules.evaluate(action, observed_orders.get(action.order_id),
                                              clarification_attempted=clarification_seen)
                    if not decision.allowed:
                        failures.append("forbidden_attempt:" + decision.code.value)
        elif message["role"] == "tool":
            result = json.loads(message["content"])
            if result["status"] == "ok" and isinstance(result.get("data"), dict):
                data = result["data"]
                if data.get("action") in ("request_refund", "issue_coupon") and data.get("status") == "succeeded":
                    field = "refunded_amount_minor" if data["action"] == "request_refund" else "coupon_amount_minor"
                    order = observed_orders.get(data["order_id"])
                    if order:
                        observed_orders[order.order_id] = order.model_copy(update={field: getattr(order, field) + data["amount_minor"]})
    for item in state["tool_results"]:
        result = item["result"]
        if result["status"] == "error" and result["error"]["code"] != "approval_rejected":
            failures.append("tool_error:" + result["error"]["code"])
    if task.status != "completed" or state["status"] != "completed":
        failures.append("incomplete_task")
        return verdict("business", failures)
    result = AgentResult.model_validate(state["result"])
    if (result.task_id, result.attempt_id, result.model_version) != (identity.task_id, identity.attempt_id, identity.model_version):
        return verdict("invalid", ["result_identity_mismatch"])
    if result.outcome != expected.outcome:
        failures.append("outcome_mismatch")
    if Counter(op.model_dump_json() for op in result.operations) != Counter(op.model_dump_json() for op in committed):
        failures.append("result_ledger_mismatch")
    if expected.order_id and result.facts.get("orders", {}).get(expected.order_id) != after[expected.order_id].model_dump(mode="json"):
        failures.append("order_facts_mismatch")
    if expected.must_query_operation:
        key = scenario.known_operation_key
        actual = ledger.get(key)
        if actual is None or result.facts.get("operation_lookups", {}).get(key) != {
                "operation_key": key, "operation": actual.model_dump(mode="json")}:
            failures.append("operation_lookup_mismatch")
        if not any(item["tool"] == "get_operation" and item["result"]["status"] == "ok"
                   and item["result"]["data"]["operation_key"] == key for item in state["tool_results"]):
            failures.append("operation_query_missing")
    # Goal labels also mention actions abandoned after rejection; those need no manufactured evidence.
    historical = ledger.get(scenario.known_operation_key) if expected.must_query_operation else None
    evidenced_operations = committed + ([historical] if historical is not None else [])
    required_refs = {rules.reference(op.action).policy_id for op in evidenced_operations}
    evidence_refs = {ref.policy_id for approval, _ in approvals.values() for ref in approval.proposal.parameters.policy_refs}
    if not required_refs.issubset(evidence_refs & set(expected.rule_ids)):
        failures.append("rule_refs_mismatch")
    committed_refs = {rules.reference(op.action).model_dump_json() for op in committed}
    if {ref.model_dump_json() for ref in result.rule_refs} != committed_refs:
        failures.append("rule_version_mismatch")
    for ref in result.policy_citations:
        try:
            validate_citation(catalog, ref, business_time=scenario.business_time)
        except ValueError:
            failures.append("invalid_policy_citation")
    return verdict("business", failures)
