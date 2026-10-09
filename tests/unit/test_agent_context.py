import copy
import json

import pytest

from tool_agent_lab.agent.context import ContextError, bounded_tool_result, encoded_size, model_context


def context_state(messages):
    return {"messages": messages, "facts": {"amount_minor": 12900},
            "operations": [{"operation_key": "original-operation", "amount_minor": 12900}],
            "citations": [{"policy_id": "refund", "policy_version": "v1"}],
            "approval_receipts": [{"proposal": {"amount_minor": 12900},
                                   "request": {"decision": "approved", "request_id": "real-receipt"}}],
            "input_receipts": [{"message": "ORD-1001"}], "clarification_attempted": True}


def test_trimming_keeps_critical_facts_and_whole_multi_tool_turn_without_mutating_trace():
    pair = [{"role": "assistant", "tool_calls": [{"id": "order-call"}, {"id": "policy-call"}]},
            {"role": "tool", "tool_call_id": "order-call", "content": "order result"},
            {"role": "tool", "tool_call_id": "policy-call", "content": "policy result"}]
    messages = [{"role": "user", "content": "Original request"}] + [
        {"role": "assistant", "content": "Old text " * 100} for _ in range(8)] + pair
    state = context_state(messages)
    original = copy.deepcopy(state)
    result = model_context(state, "Use evidence.", max_bytes=1600)
    assert len(result) < len(messages) and result[-3:] == pair
    assert result[1] == messages[0]
    assert all(value in result[0]["content"] for value in ("12900", "real-receipt", "original-operation", "v1", "ORD-1001"))
    assert encoded_size({"messages": result, "tools": []}) <= 1600
    assert state == original
    with pytest.raises(ContextError, match="critical_context_exceeds_budget"):
        model_context(state, "Use evidence.", max_bytes=100)


def test_utf8_policy_text_is_bounded_while_full_receipt_and_reference_remain():
    receipt = {"call_id": "policy-call", "status": "ok", "data": {
        "text": "\u9000\u6b3e\u653f\u7b56" * 2000,
        "reference": {"policy_id": "refund", "policy_version": "v1", "section_id": "eligibility"}}}
    original = copy.deepcopy(receipt)
    shortened = bounded_tool_result("read_policy", receipt, 512)
    assert len(shortened.encode("utf-8")) <= 512
    parsed = json.loads(shortened)
    assert parsed["truncated"] is True and parsed["data"]["text"]
    assert parsed["data"]["reference"] == receipt["data"]["reference"]
    assert receipt == original


@pytest.mark.parametrize("messages", [
    [{"role": "user", "content": "Request"}, {"role": "tool", "tool_call_id": "orphan", "content": "Result"}],
    [{"role": "assistant", "tool_calls": [{"id": "missing"}]}],
    [{"role": "assistant", "tool_calls": [{"id": "duplicate"}, {"id": "duplicate"}]},
     {"role": "tool", "tool_call_id": "duplicate", "content": "Result"}],
])
def test_unpaired_tool_history_is_explicitly_rejected(messages):
    with pytest.raises(ContextError, match="orphan_tool_result|unpaired_tool_calls"):
        model_context(context_state(messages), "Use evidence.")
