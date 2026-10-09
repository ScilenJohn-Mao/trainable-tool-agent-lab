import asyncio
import json

import httpx

from tool_agent_lab.agent.config import AgentBudgets, AgentConfig, load_agent_config
from tool_agent_lab.agent.model_client import ModelClient, ModelConfig
from tool_agent_lab.agent.nodes import AgentNodes
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.settings import load_settings


def test_output_budget_reaches_transport_and_decision_limit_stops_next_request(tmp_path):
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "response", "model": "test", "choices": [{
            "message": {"role": "assistant", "content": '{"kind":"final","summary":"Read facts."}'},
            "finish_reason": "stop",
        }]})

    settings = load_settings()
    config = load_agent_config()
    config = AgentConfig(version=config.version, prompt=config.prompt,
                         budgets=AgentBudgets(max_decisions=1, max_tokens=32))
    state = {"messages": [{"role": "user", "content": "Check the order."}], "model_calls": 0,
             "facts": {}, "operations": [], "citations": [], "approval_receipts": [],
             "input_receipts": [], "clarification_attempted": False}

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            model = ModelClient(ModelConfig(provider="http", name="test", version="test-v1",
                                base_url="http://localhost/v1", max_tokens=128), http_client=http)
            nodes = AgentNodes(tmp_path / "unused.sqlite3", settings.business_data_dir,
                               BusinessRules.from_file(settings.business_data_dir / "spec.json"), model, config)
            first = await nodes.decide(state)
            assert first["model_calls"] == 1
            assert captured[0]["max_tokens"] == 32
            assert config.prompt in captured[0]["messages"][0]["content"]
            stopped = await nodes.decide(state | {"model_calls": 1})
            assert stopped["result"]["reason"] == "decision_budget_exceeded"
            assert len(captured) == 1

    asyncio.run(run())
