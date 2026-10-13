"""Explicit scenario-aware mock fixtures for engineering checks, not model benchmarks."""

import json

from tool_agent_lab.agent.model_client import AssistantMessage, FunctionCall, ModelClient, ModelToolCall
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.evaluation.datasets import intended_action


def scripted_client(scenario, config):
    rules = BusinessRules.from_file(scenario.initial_state.data_dir / "spec.json")
    replies = []

    def tool(name, arguments):
        replies.append(AssistantMessage(tool_calls=(ModelToolCall(id="fixture-" + str(len(replies)),
            function=FunctionCall(name=name, arguments=json.dumps(arguments))),)))

    if scenario.user.input_messages:
        replies.append(AssistantMessage(content='{"kind":"ask_user","question":"Which order needs help?"}'))
    if scenario.initial_state.orders:
        tool("get_order", {"order_id": scenario.initial_state.orders[0].order_id})
    if scenario.known_operation_key:
        tool("get_operation", {"operation_key": scenario.known_operation_key})
    for index, name in enumerate(scenario.requested_actions):
        action = intended_action(name, scenario, rules).model_dump(mode="json")
        action.pop("action")
        tool(name, action)
        if scenario.user.approval_decisions[index] == "rejected":
            break
    replies.append(AssistantMessage(content='{"kind":"final","summary":"Use the recorded facts and receipts."}'))
    return ModelClient(config, mock_responses=replies)
