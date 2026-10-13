import asyncio

import httpx
import pytest

from tool_agent_lab.agent.model_client import AssistantMessage, ModelConfig, ModelReply
from tool_agent_lab.evaluation.metrics import aggregate
from tool_agent_lab.evaluation.report import build_report, write_report
from tool_agent_lab.evaluation.runner import MeasuredModel, classify_exception
from tool_agent_lab.evaluation.verifier import verdict


def attempt(category="business", failures=(), usage=None):
    return {"verdict": verdict(category, failures), "observations": {
        "model_calls": [{"usage": usage}], "tool_calls": 2,
        "seconds": {"total": 5, "model": 1, "tools": 2, "waiting": 0.5, "orchestration": 1.5}}}


def case(name, attempts, *, root=None, variant="normal", task_type="policy_refund"):
    return {"scenario_id": name, "root_scenario_id": root or name, "variant": variant, "task_type": task_type, "attempts": attempts}


def test_retry_costs_are_retained_without_inflating_denominator():
    cases = [case("one", [attempt("infrastructure", ["transport"]), attempt()]),
             case("two", [attempt("business", ["wrong_amount"])]), case("three", [attempt("invalid", ["missing"])]), case("four", [])]
    result = aggregate(cases, [a for c in cases for a in c["attempts"]])
    assert result["overall"] == {"planned": 4, "scored": 2, "successful": 1, "coverage": .5, "success_rate": .25, "scored_success_rate": .5}
    assert result["coverage_holes"] == ["three", "four"]
    assert result["costs"]["attempts"] == 4 and result["costs"]["retries"] == 1
    assert result["costs"]["seconds"]["total"] == 20
    assert result["costs"]["tokens"] is None and result["costs"]["usage_missing_calls"] == 4


def test_real_usage_sums_and_partial_usage_remains_missing():
    usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    cases = [case("a", [attempt(usage=usage)]), case("b", [attempt(usage=usage)])]
    result = aggregate(cases, [a for c in cases for a in c["attempts"]])
    assert result["costs"]["tokens"] == {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}
    cases[1]["attempts"][0]["observations"]["model_calls"][0]["usage"] = {"prompt_tokens": 10}
    result = aggregate(cases, [a for c in cases for a in c["attempts"]])
    assert result["costs"]["tokens"] is None and result["costs"]["known_tokens"]["total_tokens"] == 12


def test_fault_pairs_use_root_and_variant_not_paraphrase_family():
    cases = [case("a", [attempt()], root="root"), case("b", [attempt()], root="root", variant="timeout"),
             case("c", [attempt("invalid", ["missing"])], root="root", variant="crash"), case("d", [attempt()])]
    result = aggregate(cases, [a for c in cases for a in c["attempts"]])
    assert result["pairing"]["scored"] == 1 and result["pairing"]["both_success_rate"] == 1
    assert len(result["pairing"]["pairs"]) == 2
    assert aggregate([cases[-1]], cases[-1]["attempts"])["pairing"]["available"] is False


@pytest.mark.parametrize("error,kwargs,expected", [
    (OSError("disk"), {}, "infrastructure"),
    (ExceptionGroup("mcp", [OSError("pipe"), TimeoutError("timeout")]), {}, "infrastructure"),
    (ValueError("reply"), {"model_error": True}, "business"),
    (ValueError("exhausted"), {"model_error": True, "scripted": True}, "invalid"),
    (RuntimeError("unknown"), {}, "invalid"),
])
def test_exception_categories(error, kwargs, expected):
    assert classify_exception(error, **kwargs) == expected


def test_generation_measurement_preserves_real_usage_and_failed_call():
    class Client:
        config = ModelConfig(provider="mock", name="usage", version="usage-v1")
        async def generate(self, messages, *, tools):
            if messages:
                raise httpx.ConnectError("offline")
            return ModelReply(model_version="usage-v1", response_model="usage", response_id="reply",
                              message=AssistantMessage(content="done"), finish_reason="stop",
                              usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7})
    async def run():
        model = MeasuredModel(Client())
        await model.generate([])
        with pytest.raises(httpx.ConnectError):
            await model.generate([{}])
        assert model.calls[0]["usage"]["total_tokens"] == 7
        assert model.calls[1]["usage"] is None and model.calls[1]["error"] == "ConnectError"
        assert all(c["seconds"] >= 0 for c in model.calls)
    asyncio.run(run())


def test_report_json_round_trip_and_utf8(tmp_path):
    import json
    report = build_report({"run_id": "run", "title": "退款结果"}, [case("a", [attempt()])])
    path = tmp_path / "report.json"
    write_report(path, report)
    assert json.loads(path.read_text(encoding="utf-8")) == report
    assert b"\r\n" not in path.read_bytes()
