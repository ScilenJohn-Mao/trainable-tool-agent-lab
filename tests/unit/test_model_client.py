import asyncio
import json
import os
import shutil
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from tool_agent_lab.agent import model_client as models
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS, ToolName


@pytest.fixture
def model_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "source"
    shutil.copytree(models.PROJECT_ROOT / "configs/models", root / "configs/models")
    monkeypatch.setattr(models, "PROJECT_ROOT", root)
    for name in models.ModelConfig.model_fields:
        monkeypatch.delenv(f"TTAL_MODEL_{name.upper()}", raising=False)
    return root


def completion(message: dict, *, reason: str = "stop") -> dict:
    return {
        "id": "chat-response-1", "model": "served-qwen3b",
        "choices": [{"index": 0, "message": message, "finish_reason": reason}],
        "usage": {"prompt_tokens": 32, "completion_tokens": 12, "total_tokens": 44},
    }


def test_config_defaults_and_process_override_dotenv_without_writes(
    model_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (model_root / ".env").write_text(
        "TTAL_MODEL_MAX_TOKENS=128\nTTAL_MODEL_API_KEY=fixture-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TTAL_MODEL_MAX_TOKENS", "64")
    before = dict(os.environ)
    files_before = set(model_root.rglob("*"))
    config = models.load_model_config("configs/models/qwen3b.yaml")
    assert config.provider == "http"
    assert config.name == "Qwen/Qwen2.5-3B-Instruct"
    assert config.version == "qwen2.5-3b-base-v1"
    assert config.max_tokens == 64
    assert config.api_key.get_secret_value() == "fixture-secret"
    assert "fixture-secret" not in config.model_dump_json() + repr(config)
    assert "api_key" not in config.model_dump()
    assert dict(os.environ) == before
    assert set(model_root.rglob("*")) == files_before


def test_config_paths_ignore_cwd_and_custom_credential_variable(
    model_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (model_root / "chosen.env").write_text(
        "TTAL_MODEL_BASE_URL=http://localhost:9123/api/v1\n"
        "TTAL_MODEL_API_KEY_ENV=EXAMPLE_MODEL_KEY\nEXAMPLE_MODEL_KEY=chosen-secret\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    config = models.load_model_config("configs/models/qwen3b.yaml", env_file="chosen.env")
    assert str(config.base_url) == "http://localhost:9123/api/v1"
    assert config.api_key.get_secret_value() == "chosen-secret"
    with pytest.raises(FileNotFoundError):
        models.load_model_config("configs/models/missing.yaml")
    with pytest.raises(FileNotFoundError):
        models.load_model_config(env_file="missing.env")


@pytest.mark.parametrize("values", [
    {"provider": "http"}, {"max_tokens": 0}, {"temperature": float("nan")},
    {"timeout_seconds": 0}, {"provider": "http", "base_url": "file:///model"},
])
def test_invalid_configuration_is_rejected(values: dict) -> None:
    with pytest.raises(ValidationError):
        models.ModelConfig.model_validate({
            "provider": "mock", "name": "example", "version": "v1", **values,
        })


def test_yaml_cannot_store_credentials(model_root: Path) -> None:
    path = model_root / "configs/models/mock.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "api_key: forbidden\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Use api_key_env"):
        models.load_model_config()


def test_scripted_mock_text_tool_call_and_exhaustion(model_root: Path) -> None:
    call = models.ModelToolCall(id="call-order", function=models.FunctionCall(
        name="get_order", arguments='{"order_id":"ORD-1001"}',
    ))
    client = models.ModelClient(models.load_model_config(), mock_responses=[
        models.AssistantMessage(tool_calls=(call,)),
        models.AssistantMessage(content="Please confirm the proposed action."),
    ])

    async def run() -> None:
        first = await client.generate([{"role": "user", "content": "Check my order"}])
        assert first.response_id == "mock-1" and first.model_version == "mock-v1"
        assert first.usage is None and first.finish_reason == "tool_calls"
        assert first.message.as_message()["tool_calls"][0]["id"] == "call-order"
        assert first.message.tool_calls[0] == call
        second = await client.generate([
            first.message.as_message(),
            {"role": "tool", "tool_call_id": "call-order", "content": "Order found"},
        ])
        assert second.response_id == "mock-2" and second.finish_reason == "stop"
        assert second.message.content == "Please confirm the proposed action."
        assert "tool_calls" not in second.message.as_message()
        with pytest.raises(ValueError, match="exhausted"):
            await client.generate([])
        other = models.ModelClient(models.load_model_config())
        with pytest.raises(ValueError, match="exhausted"):
            await other.generate([])

    asyncio.run(run())


def test_http_passes_history_shared_tool_schemas_and_configured_limits(model_root: Path) -> None:
    config = models.load_model_config("configs/models/qwen3b.yaml").model_copy(update={
        "max_tokens": 96, "temperature": 0.25, "timeout_seconds": 7.5,
    })
    history = [
        {"role": "system", "content": "Check orders; human approval is required."},
        {"role": "user", "content": "Please refund ORD-1001"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-order", "type": "function",
            "function": {"name": "get_order", "arguments": '{"order_id":"ORD-1001"}'},
        }]},
        {"role": "tool", "tool_call_id": "call-order", "content": '{"paid_minor":12900}'},
    ]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        assert request.method == "POST"
        assert str(request.url) == "http://127.0.0.1:8001/v1/chat/completions"
        assert "authorization" not in request.headers
        assert request.extensions["timeout"]["read"] == 7.5
        assert body["messages"] == history
        assert body["model"] == config.name
        assert body["max_tokens"] == 96 and body["temperature"] == 0.25
        assert body["stream"] is False and body["n"] == 1
        assert body["tool_choice"] == "auto"
        refund = next(t["function"] for t in body["tools"] if t["function"]["name"] == "request_refund")
        assert refund["parameters"] == TOOL_CONTRACTS[ToolName.REQUEST_REFUND].argument_model.model_json_schema()
        assert not ({"owner_id", "approval_id", "execution_context", "operation_key"} & refund["parameters"]["properties"].keys())
        return httpx.Response(200, json=completion({"role": "assistant", "content": "Please confirm."}))

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = models.ModelClient(config, http_client=http)
            reply = await client.generate(history, tools=tuple(TOOL_CONTRACTS.values()))
            assert not http.is_closed
        assert reply.model_version == config.version and reply.response_model == "served-qwen3b"
        assert reply.response_id == "chat-response-1"
        assert reply.usage["total_tokens"] == 44 and reply.message.content == "Please confirm."
    asyncio.run(run())
    assert len(seen) == 1


def test_http_preserves_tool_ids_and_raw_invalid_arguments_for_caller(model_root: Path) -> None:
    message = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call-1", "type": "function", "function": {
            "name": "get_order", "arguments": '  {"order_id":"ORD-1001"}  ',
        }},
        {"id": "call-2", "type": "function", "function": {
            "name": "unknown_tool", "arguments": "{broken-json",
        }},
    ]}

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=completion(message, reason="tool_calls")),
        )) as http:
            reply = await models.ModelClient(
                models.load_model_config("configs/models/qwen3b.yaml"), http_client=http,
            ).generate([{"role": "user", "content": "Check order"}])
        assert reply.message.as_message() == message
        assert [call.id for call in reply.message.tool_calls] == ["call-1", "call-2"]
        with pytest.raises(json.JSONDecodeError):
            json.loads(reply.message.tool_calls[1].function.arguments)
    asyncio.run(run())


def test_http_auth_and_truncated_text_are_preserved(model_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTAL_MODEL_API_KEY", "fixture-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fixture-key"
        assert "tools" not in json.loads(request.content)
        return httpx.Response(200, json=completion({"role": "assistant", "content": "Partial"}, reason="length"))

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            reply = await models.ModelClient(
                models.load_model_config("configs/models/qwen3b.yaml"), http_client=http,
            ).generate([{"role": "user", "content": "Explain"}])
        assert reply.finish_reason == "length" and reply.message.content == "Partial"
        assert "fixture-key" not in reply.model_dump_json()
    asyncio.run(run())


@pytest.mark.parametrize("kind,error_type", [
    ("http", httpx.HTTPStatusError), ("redirect", httpx.HTTPStatusError),
    ("timeout", httpx.ReadTimeout), ("json", json.JSONDecodeError),
    ("empty_choices", ValidationError), ("missing_message", ValidationError),
])
def test_http_failures_propagate_without_retry_or_mock_fallback(
    model_root: Path, kind: str, error_type: type[Exception],
) -> None:
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if kind == "http":
            return httpx.Response(503, json={"error": "unavailable"})
        if kind == "redirect":
            return httpx.Response(307, headers={"location": "http://other-server/v1/chat/completions"})
        if kind == "timeout":
            raise httpx.ReadTimeout("model timeout", request=request)
        if kind == "json":
            return httpx.Response(200, content=b"not-json")
        body = completion({"role": "assistant", "content": "ok"})
        if kind == "empty_choices":
            body["choices"] = []
        else:
            body["choices"][0]["message"] = {"role": "assistant"}
        return httpx.Response(200, json=body)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = models.ModelClient(
                models.load_model_config("configs/models/qwen3b.yaml"), http_client=http,
                mock_responses=[models.AssistantMessage(content="Must not fall back")],
            )
            with pytest.raises(error_type):
                await client.generate([{"role": "user", "content": "Hello"}])
    asyncio.run(run())
    assert len(attempts) == 1


def test_cli_inspection_and_mock_call_have_no_business_effects(model_root: Path, capsys) -> None:
    before = set(model_root.rglob("*"))
    assert models.main(["--config", "configs/models/qwen3b.yaml"]) == 0
    assert json.loads(capsys.readouterr().out)["provider"] == "http"
    assert models.main(["--message", "Please refund ORD-1001", "--with-tools"]) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["model_version"] == "mock-v1"
    assert reply["message"]["content"] == "Mock model ready. No business operation was executed."
    assert reply["message"]["tool_calls"] == []
    assert set(model_root.rglob("*")) == before
