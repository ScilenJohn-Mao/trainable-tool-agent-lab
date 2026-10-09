"""Bound model-visible history without discarding critical facts or tool pairs."""

import copy
import json

from tool_agent_lab.agent.model_client import chat_tool_definitions


class ContextError(ValueError):
    pass


def encoded_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def bounded_tool_result(name: str, result: dict, max_bytes: int) -> str:
    value = copy.deepcopy(result)
    if encoded_size(value) > max_bytes and value["status"] == "ok":
        data = value["data"]
        targets = [(data, "text")] if name == "read_policy" else (
            [(hit, "excerpt") for hit in data["hits"]] if name == "search_policy" else []
        )
        while targets and encoded_size(value) > max_bytes:
            target, field = max(targets, key=lambda item: len(item[0][item[1]].encode("utf-8")))
            raw = target[field].encode("utf-8")
            if len(raw) <= 1:
                break
            target[field] = raw[:len(raw) // 2].decode("utf-8", errors="ignore")
            value["truncated"] = True
    if encoded_size(value) > max_bytes:
        raise ContextError("critical_tool_output_exceeds_budget")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def history_blocks(messages: list[dict]) -> list[list[dict]]:
    blocks, position = [], 0
    while position < len(messages):
        message = messages[position]
        block = [message]
        position += 1
        calls = message.get("tool_calls", [])
        if calls:
            expected = [call["id"] for call in calls]
            actual = []
            while position < len(messages) and messages[position]["role"] == "tool":
                actual.append(messages[position]["tool_call_id"])
                block.append(messages[position])
                position += 1
            if len(set(expected)) != len(expected) or sorted(expected) != sorted(actual):
                raise ContextError("unpaired_tool_calls")
        elif message["role"] == "tool":
            raise ContextError("orphan_tool_result")
        blocks.append(block)
    return blocks


def critical_facts(state: dict) -> dict:
    return {
        "facts": state["facts"], "operations": state["operations"],
        "policy_citations": state["citations"],
        "human_decisions": [{"proposal": r["proposal"], "decision": r["request"]["decision"],
                             "request_id": r["request"]["request_id"]} for r in state["approval_receipts"]],
        "supplemental_inputs": state["input_receipts"],
        "clarification_attempted": state["clarification_attempted"],
    }


def model_context(state: dict, system_prompt: str, *, tools=(), max_bytes: int = 24000) -> list[dict]:
    system = {"role": "system", "content": system_prompt + "\nRecorded facts (not authorization):\n" +
              json.dumps(critical_facts(state), ensure_ascii=False, separators=(",", ":"))}
    blocks = history_blocks(state["messages"])
    if not blocks:
        raise ContextError("empty_history")
    first, recent = blocks[0], blocks[1:]
    definitions = chat_tool_definitions(tools)
    while True:
        messages = [system] + first + [message for block in recent for message in block]
        if encoded_size({"messages": messages, "tools": definitions}) <= max_bytes:
            return copy.deepcopy(messages)
        if len(recent) <= 1:
            raise ContextError("critical_context_exceeds_budget")
        recent.pop(0)
