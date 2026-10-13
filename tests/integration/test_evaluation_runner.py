import asyncio
import json

import httpx
import pytest

from scripts.evaluation_fixtures import scripted_client
from tool_agent_lab.agent.config import load_agent_config
from tool_agent_lab.agent.model_client import AssistantMessage, ModelClient, ModelConfig
from tool_agent_lab.evaluation.datasets import load_dataset
from tool_agent_lab.evaluation.runner import evaluate
from tool_agent_lab.schemas.scenarios import ExperimentConfig


def experiment():
    return ExperimentConfig(model=ModelConfig(provider="mock", name="evaluation-fixtures", version="evaluation-fixtures-v1"),
                            agent=load_agent_config())


def test_shared_graph_real_mcp_four_classes_and_held_out_policy(tmp_path):
    rows = load_dataset("data/scenarios/v1", "test")
    selections = [("refund_boundary", "business-v1"), ("combined_partial", "business-v1"), ("lookup_coupon", "business-v1"),
                  ("digital_late", "business-v1"), ("combined", "business-v2"), ("lookup", "business-v2")]
    ids = [next(s.scenario_id for s in rows if (s.mode, s.fixture_version) == wanted) for wanted in selections]
    config = experiment()
    output = tmp_path / "report.json"
    report = asyncio.run(evaluate("data/scenarios/v1", "test", config, scenario_ids=ids, output=output,
        root=tmp_path / "runtime", model_factory=lambda s: scripted_client(s, config.model)))
    assert report["metrics"]["overall"]["successful"] == 6, [c["attempts"][-1]["verdict"] for c in report["cases"]]
    assert len(report["metrics"]["by_task_type"]) == 4
    assert report["measurement"] == "scripted_engineering_check" and report["status"] == "completed"
    assert report["metrics"]["costs"]["tokens"] is None
    assert report["metrics"]["pairing"]["available"] is False
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert not list((tmp_path / "runtime").iterdir())
    for case in report["cases"]:
        attempt = case["attempts"][0]
        assert attempt["observations"]["seconds"]["tools"] > 0
        assert attempt["trajectory"]["business"]["approvals"]
    with pytest.raises(FileExistsError):
        asyncio.run(evaluate("data/scenarios/v1", "test", config, scenario_ids=ids, output=output,
                            model_factory=lambda s: scripted_client(s, config.model)))


def test_infrastructure_retry_and_exhaustion_keep_coverage_and_all_attempts(tmp_path):
    scenario = next(s for s in load_dataset("data/scenarios/v1", "validation") if s.mode == "query")
    config = experiment()
    calls = 0
    class Offline(ModelClient):
        async def generate(self, messages, *, tools=()):
            raise httpx.ConnectError("offline test endpoint")
    def factory(s):
        nonlocal calls
        calls += 1
        return Offline(config.model) if calls == 1 else scripted_client(s, config.model)
    report = asyncio.run(evaluate("data/scenarios/v1", "validation", config, scenario_ids=[scenario.scenario_id],
        infrastructure_retries=1, model_factory=factory, root=tmp_path / "retried"))
    attempts = report["cases"][0]["attempts"]
    assert [a["verdict"]["reward"] for a in attempts] == [None, 1]
    assert attempts[0]["trajectory"] is not None and attempts[0]["error"]["type"] == "ConnectError"
    assert report["metrics"]["costs"]["retries"] == 1 and report["metrics"]["overall"]["planned"] == 1
    report = asyncio.run(evaluate("data/scenarios/v1", "validation", config, scenario_ids=[scenario.scenario_id],
        infrastructure_retries=1, model_factory=lambda s: Offline(config.model), root=tmp_path / "exhausted"))
    assert report["metrics"]["coverage_holes"] == [scenario.scenario_id]
    assert len(report["cases"][0]["attempts"]) == 2
    assert report["metrics"]["overall"]["scored"] == 0


def test_model_early_conclusion_is_zero_and_missing_mock_script_is_unscorable(tmp_path):
    scenario = next(s for s in load_dataset("data/scenarios/v1", "validation") if s.mode == "refund")
    config = experiment()
    report = asyncio.run(evaluate("data/scenarios/v1", "validation", config, scenario_ids=[scenario.scenario_id],
        model_factory=lambda s: ModelClient(config.model, mock_responses=[AssistantMessage(content="Refund done")]), root=tmp_path / "early"))
    assert report["cases"][0]["attempts"][0]["verdict"]["reward"] == 0
    report = asyncio.run(evaluate("data/scenarios/v1", "validation", config, scenario_ids=[scenario.scenario_id],
        model_factory=lambda s: ModelClient(config.model), root=tmp_path / "missing"))
    assert report["cases"][0]["attempts"][0]["verdict"]["category"] == "invalid"
    assert report["metrics"]["coverage_holes"] == [scenario.scenario_id]
