import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tool_agent_lab.agent.model_client import ModelClient, ModelConfig
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS, ToolName


@pytest.fixture
def chat_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            if len(body["messages"]) == 1:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "tcp-order-1", "type": "function", "function": {
                        "name": "get_order", "arguments": '{"order_id":"ORD-1001"}',
                    },
                }]}
                reason = "tool_calls"
            else:
                message = {"role": "assistant", "content": "Order located. Human confirmation is required for a refund."}
                reason = "stop"
            data = json.dumps({
                "id": f"tcp-response-{len(requests)}", "model": "local-chat-fixture",
                "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_tcp_chat_and_tool_result_roundtrip(chat_server) -> None:
    base_url, requests = chat_server
    client = ModelClient(ModelConfig(
        provider="http", name="local-chat-fixture", version="fixture-http-v1",
        base_url=base_url, max_tokens=64, timeout_seconds=5,
    ))

    async def run() -> None:
        messages = [{"role": "user", "content": "Check ORD-1001"}]
        tools = [TOOL_CONTRACTS[ToolName.GET_ORDER]]
        first = await client.generate(messages, tools=tools)
        assert first.model_version == "fixture-http-v1" and first.response_model == "local-chat-fixture"
        assert first.message.tool_calls[0].id == "tcp-order-1"
        arguments = TOOL_CONTRACTS[ToolName.GET_ORDER].parse_arguments(
            json.loads(first.message.tool_calls[0].function.arguments),
        )
        assert arguments.order_id == "ORD-1001"
        messages.extend([
            first.message.as_message(),
            {"role": "tool", "tool_call_id": "tcp-order-1", "content": '{"paid_minor":12900}'},
        ])
        second = await client.generate(messages, tools=tools)
        assert second.finish_reason == "stop"
        assert second.response_id == "tcp-response-2" and second.usage["total_tokens"] == 15
        assert "Human confirmation" in second.message.content
    asyncio.run(run())
    assert len(requests) == 2
    assert all(path == "/v1/chat/completions" for path, body in requests)
    assert requests[1][1]["messages"][2]["tool_call_id"] == "tcp-order-1"
    assert requests[0][1]["max_tokens"] == 64
