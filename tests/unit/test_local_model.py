"""Check the direct model boundary without importing GPU libraries."""

import asyncio
import copy
import json
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from tool_agent_lab.agent import local_model as local
from tool_agent_lab.agent import model_client as models
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS, ToolName


def config(base: Path, **changes) -> models.ModelConfig:
    return models.ModelConfig(
        provider="local", name="Qwen/Qwen2.5-3B-Instruct", version="local-test",
        base_path=base, max_tokens=8, context_tokens=64, **changes,
    )


def test_local_paths_and_overrides_are_project_relative(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / "local.yaml").write_text(
        "provider: local\nname: qwen\nversion: v1\nbase_path: models/base/qwen\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("TTAL_MODEL_ADAPTER_PATH=models/adapters/old\n", encoding="utf-8")
    for name in models.ModelConfig.model_fields:
        monkeypatch.delenv(f"TTAL_MODEL_{name.upper()}", raising=False)
    monkeypatch.setenv("TTAL_MODEL_ADAPTER_PATH", "models/adapters/new")
    monkeypatch.setenv("TTAL_MODEL_CONTEXT_TOKENS", "2048")
    monkeypatch.setattr(models, "PROJECT_ROOT", root)
    monkeypatch.chdir(tmp_path)
    value = models.load_model_config("local.yaml")
    assert value.base_path == root / "models/base/qwen"
    assert value.adapter_path == root / "models/adapters/new"
    assert value.context_tokens == 2048


@pytest.mark.parametrize("changes", [
    {"base_path": None}, {"device": "cpu"}, {"context_tokens": 8},
])
def test_invalid_local_budget_or_device(changes):
    values = dict(provider="local", name="qwen", version="v1", base_path=Path("models/base"), max_tokens=8)
    with pytest.raises(ValidationError):
        models.ModelConfig(**(values | changes))


def test_qwen_text_and_multiple_calls_keep_raw_arguments_and_ids():
    assert local.parse_qwen_output("Please provide the order number.").content.startswith("Please")
    raw = '{ "order_id" : "ORD-1001" }'
    message = local.parse_qwen_output(
        'Checking. <tool_call>{"name":"get_order","arguments":' + raw + '}</tool_call>'
        '<tool_call>{"arguments":{"x":{"arguments":1}},"name":"unknown_tool"}</tool_call>'
    )
    assert message.content == "Checking."
    assert message.tool_calls[0].function.arguments == raw
    assert message.tool_calls[1].function.name == "unknown_tool"
    assert message.tool_calls[1].function.arguments == '{"x":{"arguments":1}}'
    assert len({call.id for call in message.tool_calls}) == 2
    history = [message.as_message(), {"role": "tool", "tool_call_id": message.tool_calls[0].id, "content": "found"}]
    original = copy.deepcopy(history)
    converted = local.template_messages(history)
    assert converted[0]["tool_calls"][0]["function"]["arguments"] == {"order_id": "ORD-1001"}
    assert converted[1] == history[1] and history == original


@pytest.mark.parametrize("kind,field", [("ask_user", "question"), ("final", "summary")])
@pytest.mark.parametrize("wrapper", ["JSON {}", "Explanation. <tool_call>{}</tool_call>"])
def test_qwen_control_reply_framing_remains_content_for_graph_validation(kind, field, wrapper):
    control = {"kind": kind, field: "Please provide the order ID."}
    message = local.parse_qwen_output(wrapper.format(json.dumps(control)))
    assert not message.tool_calls
    assert json.loads(message.content) == control


def test_qwen_repeated_opening_tag_preserves_actual_tool_arguments():
    raw = '{"query":"refund","category":"general_goods","limit":2}'
    reply = local.parse_qwen_output('<tool_call>\n<tool_call>\n{"name":"search_policy","arguments":' + raw + '}</tool_call>')
    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0].function.name == "search_policy"
    assert reply.tool_calls[0].function.arguments == raw


@pytest.mark.parametrize("text", [
    '<tool_call>{"name":"get_order"}</tool_call>',
    '<tool_call>not json</tool_call>',
    '<tool_call>{"name":"get_order",',
])
def test_invalid_complete_output_is_not_a_valid_tool_call(text):
    with pytest.raises(ValueError):
        local.parse_qwen_output(text)


def test_truncated_envelope_is_text_for_caller_handling():
    text = '<tool_call>{"name":"get_order",'
    message = local.parse_qwen_output(text, truncated=True)
    assert message.content == text and not message.tool_calls


@pytest.fixture
def model_stack(tmp_path, monkeypatch):
    base = tmp_path / "base"
    adapter = tmp_path / "adapter"
    base.mkdir()
    adapter.mkdir()
    (base / "config.json").write_text("{}", encoding="utf-8")
    (base / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    (adapter / "adapter_config.json").write_text("{}", encoding="utf-8")
    records = []
    state = {"prompt_tokens": 12, "tokens": [20, 21, 99], "text": "Please confirm."}

    class Tokens(list):
        def __getitem__(self, item):
            value = super().__getitem__(item)
            return Tokens(value) if isinstance(item, slice) else value

        def tolist(self):
            return list(self)

    class Inputs(dict):
        def to(self, device):
            records.append(("inputs-device", device))
            return self

    class Tokenizer:
        chat_template = "base-template"
        pad_token_id = 0

        def apply_chat_template(self, messages, **kwargs):
            records.append(("template", messages, kwargs))
            return Inputs(input_ids=SimpleNamespace(shape=(1, state["prompt_tokens"])))

        def decode(self, tokens, **kwargs):
            records.append(("decode", tokens, kwargs))
            return state["text"]

    class Model:
        device = "cuda:0"
        generation_config = SimpleNamespace(eos_token_id=[98, 99])

        def eval(self):
            records.append(("eval",))

        def generate(self, **kwargs):
            records.append(("generate", kwargs))
            return [Tokens([1] * state["prompt_tokens"] + state["tokens"])]

    def tokenizer_load(path, **kwargs):
        records.append(("tokenizer-load", path, kwargs))
        return Tokenizer()

    def model_load(path, **kwargs):
        records.append(("model-load", path, kwargs))
        return Model()

    def adapter_load(model, path, **kwargs):
        records.append(("adapter-load", path, kwargs))
        return model

    torch = SimpleNamespace(
        float32="float32", float16="float16", inference_mode=nullcontext,
        cuda=SimpleNamespace(is_available=lambda: True),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "torch.nn.attention", SimpleNamespace(
        SDPBackend=SimpleNamespace(FLASH_ATTENTION="flash", EFFICIENT_ATTENTION="efficient",
                                   CUDNN_ATTENTION="cudnn", MATH="math"),
        sdpa_kernel=lambda *args, **kwargs: nullcontext(),
    ))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=tokenizer_load),
        AutoModelForCausalLM=SimpleNamespace(from_pretrained=model_load),
        BitsAndBytesConfig=lambda **kwargs: kwargs,
    ))
    monkeypatch.setitem(sys.modules, "peft", SimpleNamespace(
        PeftModel=SimpleNamespace(from_pretrained=adapter_load),
    ))
    return SimpleNamespace(base=base, adapter=adapter, records=records, state=state, torch=torch)


def test_direct_client_reuses_model_and_fills_tool_result_without_http(model_stack):
    stack = model_stack
    client_config = config(stack.base, adapter_path=stack.adapter)
    tool = TOOL_CONTRACTS[ToolName.GET_ORDER]

    async def run():
        async def forbidden(request):
            pytest.fail("Local inference must never call HTTP")

        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
            client = models.ModelClient(client_config, http_client=http)
            stack.state["text"] = '<tool_call>{"name":"get_order","arguments":{"order_id":"ORD-1001"}}</tool_call>'
            first = await client.generate([{"role": "user", "content": "Check ORD-1001"}], tools=[tool])
            assert first.finish_reason == "tool_calls" and first.model_version == "local-test"
            call = first.message.tool_calls[0]
            args = tool.parse_arguments(json.loads(call.function.arguments))
            assert args.order_id == "ORD-1001"
            stack.state["text"] = "Order found. Please confirm the proposal."
            second = await client.generate([
                first.message.as_message(),
                {"role": "tool", "tool_call_id": call.id, "content": '{"paid_amount_minor":12900}'},
            ], tools=[tool])
            assert second.finish_reason == "stop"
            assert second.usage == {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
            assert second.response_id != first.response_id

    asyncio.run(run())
    loads = [entry for entry in stack.records if entry[0] == "model-load"]
    assert len(loads) == 1
    assert loads[0][1] == stack.base
    assert loads[0][2]["local_files_only"] is True and loads[0][2]["trust_remote_code"] is False
    assert loads[0][2]["quantization_config"]["load_in_4bit"] is True
    adapter_load = next(entry for entry in stack.records if entry[0] == "adapter-load")
    assert adapter_load[1] == stack.adapter and adapter_load[2] == {"local_files_only": True, "is_trainable": False}
    templates = [entry for entry in stack.records if entry[0] == "template"]
    assert templates[0][2]["tools"] == models.chat_tool_definitions([tool])
    assert templates[1][1][0]["tool_calls"][0]["function"]["arguments"] == {"order_id": "ORD-1001"}
    assert templates[1][1][1]["content"] == '{"paid_amount_minor":12900}'
    generate = next(entry for entry in stack.records if entry[0] == "generate")[1]
    assert generate["max_new_tokens"] == 8 and generate["max_time"] == 60
    assert generate["do_sample"] is False and "temperature" not in generate
    decode = next(entry for entry in stack.records if entry[0] == "decode")
    assert decode[1] == [20, 21] and decode[2] == {"skip_special_tokens": False}


@pytest.mark.parametrize("tokens,reason", [([20] * 8, "length"), ([20], "timeout")])
def test_generation_budget_and_early_stop_are_visible(model_stack, tokens, reason):
    model_stack.state.update(tokens=tokens, text='<tool_call>{"name":')
    value = local.LocalModel(config(model_stack.base, temperature=0.3))
    reply = value.generate([{"role": "user", "content": "Check"}])
    assert reply.finish_reason == reason and not reply.message.tool_calls
    assert reply.usage["completion_tokens"] == len(tokens)
    generate = next(entry for entry in model_stack.records if entry[0] == "generate")[1]
    assert generate["temperature"] == 0.3 and generate["do_sample"] is True


def test_context_overflow_does_not_silently_truncate_or_generate(model_stack):
    model_stack.state["prompt_tokens"] = 60
    with pytest.raises(ValueError, match="context_tokens"):
        local.LocalModel(config(model_stack.base)).generate([{"role": "user", "content": "long"}])
    assert not any(entry[0] == "generate" for entry in model_stack.records)


def test_cuda_unavailable_does_not_fall_back(model_stack):
    model_stack.torch.cuda.is_available = lambda: False
    with pytest.raises(RuntimeError, match="CUDA device is unavailable"):
        local.LocalModel(config(model_stack.base)).generate([{"role": "user", "content": "hello"}])
    assert model_stack.records == []


def test_missing_model_configuration_fails_before_loading(model_stack):
    (model_stack.base / "config.json").unlink()
    with pytest.raises(FileNotFoundError, match="config.json"):
        local.LocalModel(config(model_stack.base)).generate([{"role": "user", "content": "hello"}])
    assert model_stack.records == []


def test_float32_cpu_does_not_request_quantization(model_stack):
    local.LocalModel(config(model_stack.base, device="cpu", precision="float32")).generate(
        [{"role": "user", "content": "hello"}],
    )
    load = next(entry for entry in model_stack.records if entry[0] == "model-load")[2]
    assert load["dtype"] == "float32" and load["device_map"] == {"": "cpu"}
    assert "quantization_config" not in load


def test_import_and_missing_asset_failure_do_not_import_heavy_libraries(tmp_path):
    source = '''
import asyncio, builtins, sys
from pathlib import Path
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'torch', 'transformers', 'peft', 'art', 'bitsandbytes'}:
        raise AssertionError('Unexpected heavy import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from tool_agent_lab.agent.model_client import ModelClient, ModelConfig
from tool_agent_lab.agent.local_model import parse_qwen_output
assert parse_qwen_output('hello').content == 'hello'
client = ModelClient(ModelConfig(provider='local', name='qwen', version='v1', base_path=Path(sys.argv[1])))
try:
    asyncio.run(client.generate([{'role': 'user', 'content': 'hello'}]))
except FileNotFoundError as error:
    assert 'Local model directory does not exist' in str(error)
else:
    raise AssertionError('Missing files must fail')
print('lightweight import and explicit missing-file failure passed')
'''
    result = subprocess.run(
        [sys.executable, "-c", source, str(tmp_path / "missing")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
