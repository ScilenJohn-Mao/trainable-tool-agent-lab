"""Verify read-only tool/result pairing without loading model libraries."""

import asyncio
import hashlib
import importlib.util

from scripts.demo_local_tools import readonly_roundtrip
from scripts.seed_demo import seed_demo
from tool_agent_lab.agent.model_client import AssistantMessage, FunctionCall, ModelClient, ModelConfig, ModelToolCall
from tool_agent_lab.settings import load_settings
from tool_agent_lab.tools.client import local_server_parameters, open_tool_session


def model(*messages):
    return ModelClient(ModelConfig(provider="mock", name="mock", version="mock-demo"), mock_responses=messages)


def test_real_mcp_result_is_paired_and_next_model_call_receives_facts(tmp_path):
    database = tmp_path / "app.sqlite3"
    seed_demo(database, settings=load_settings())
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    client = model(AssistantMessage(tool_calls=(ModelToolCall(
        id="call-order", function=FunctionCall(name="get_order", arguments='{"order_id":"ORD-1001"}'),
    ),)), AssistantMessage(content="12900 fen"))
    original_generate = client.generate
    requests = []

    async def capture(messages, **kwargs):
        requests.append(list(messages))
        return await original_generate(messages, **kwargs)

    client.generate = capture

    async def run():
        async with open_tool_session(local_server_parameters(database=database, owner_id="demo-user")) as tools:
            return await readonly_roundtrip(client, tools)

    report = asyncio.run(run())
    assert report["finish_reason"] == "answered" and report["tool_result_roundtrip"]
    assert report["tool_results"][0]["result"]["data"]["paid_amount_minor"] == 12900
    returned = requests[1][-1]
    assert returned["role"] == "tool" and returned["tool_call_id"] == "call-order"
    assert '"paid_amount_minor": 12900' in returned["content"]
    assert requests[1][-2]["tool_calls"][0]["id"] == "call-order"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert importlib.util.find_spec("torch") is None


def test_write_selection_stops_before_tool_execution():
    client = model(AssistantMessage(tool_calls=(ModelToolCall(
        id="call-write", function=FunctionCall(name="request_refund", arguments="{}"),
    ),)))

    class ForbiddenTools:
        async def call_tool(self, *args, **kwargs):
            raise AssertionError("A read-only demo must never execute a write tool")

    report = asyncio.run(readonly_roundtrip(client, ForbiddenTools()))
    assert report["finish_reason"] == "write_tool_not_allowed"
    assert report["tool_results"] == []


def test_model_failure_is_preserved_without_retry():
    client = model(AssistantMessage(content="partial"))

    async def fail(*args, **kwargs):
        raise ValueError("input exceeds context budget")

    client.generate = fail
    report = asyncio.run(readonly_roundtrip(client, None))
    assert report["finish_reason"] == "error"
    assert report["error"] == {"type": "ValueError", "message": "input exceeds context budget"}
    assert report["turns"] == []
