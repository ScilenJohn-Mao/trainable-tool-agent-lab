import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from art_trainlab.adapter import ARTModelClient
from art_trainlab.config import TrainingConfig, load_config, offline_environment
from art_trainlab.recorder import IntegrityError, Recorder, compare_art_sequences, token_data, verify_group
from art_trainlab.rollout import ARTInterfaces, sample_group
from art_trainlab.train import backend_model_arguments, check, preflight
from scripts.evaluation_fixtures import scripted_client
from tool_agent_lab.agent.model_client import AssistantMessage, ModelClient
from tool_agent_lab.evaluation.datasets import load_dataset
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS


def completion(message=None, *, response_id="completion"):
    """Synthetic wire fixture; these token IDs/logprobs are not actual model evidence."""
    return {"id": response_id, "model": "fixture-model", "choices": [{"index": 0, "finish_reason": "stop",
        "message": message or {"role": "assistant", "content": "done"},
        "prompt_token_ids": [10, 11], "token_ids": [20, 21],
        "logprobs": {"content": [{"token": "token_id:20", "logprob": -.1}, {"token": "token_id:21", "logprob": -.2}]}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}}


def test_fixed_cuda_profile_and_matching_torchvision_lock(monkeypatch):
    import os
    import tomllib

    keys = ("ART_VLLM_RUNTIME_CUDA_PROFILE", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE",
            "HF_DATASETS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY", "WANDB_MODE", "WEAVE_DISABLED",
            "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "WANDB_API_KEY", "WEAVE_API_KEY", "LANGSMITH_API_KEY")
    for key in keys:
        monkeypatch.setenv(key, "test-value")
    monkeypatch.setenv("ART_VLLM_RUNTIME_CUDA_PROFILE", "cuda13")
    offline_environment()
    assert os.environ["ART_VLLM_RUNTIME_CUDA_PROFILE"] == "cuda12"
    root = Path(__file__).resolve().parents[2] / "training/art"
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    vision = next(p for p in lock["package"] if p["name"] == "torchvision")
    assert vision["version"] == "0.26.0+cu128"
    assert vision["source"]["registry"] == "https://download.pytorch.org/whl/cu128"


def record(response=None):
    return {"request_id": "request", "model_version": "policy@0", "requested_model": "fixture-model", "tools": [],
            "messages": [{"role": "user", "content": "hello"}], "response": response or completion()}


def trace(records):
    return {"state": {"identity": {"model_version": "policy@0", "task_id": "task", "thread_id": "thread"},
                      "model_calls": len(records), "messages": [{"role": "user", "content": "hello"}] +
                      [r["response"]["choices"][0]["message"] for r in records]}}


@pytest.mark.parametrize("mutation,reason", [
    (lambda r: r["response"]["choices"][0].pop("token_ids"), "missing_token_ids"),
    (lambda r: r["response"]["choices"][0].pop("logprobs"), "missing_logprobs"),
    (lambda r: r["response"]["choices"][0]["logprobs"]["content"][0].update(logprob=float("nan")), "invalid_logprob"),
    (lambda r: r["response"]["choices"][0]["logprobs"]["content"][0].update(token="token_id:999"), "logprob_token_mismatch"),
    (lambda r: r["response"]["usage"].update(completion_tokens=3), "usage_token_mismatch"),
])
def test_missing_or_misaligned_tokens_are_rejected(mutation, reason):
    data = record()
    mutation(data)
    with pytest.raises(IntegrityError, match=reason):
        token_data(data)


def test_durable_deduplication_and_conflicting_response_id(tmp_path):
    path = tmp_path / "completions.jsonl"
    recorder = Recorder(path)
    recorder.add(record())
    recorder.add(record())
    restored = Recorder(path)
    assert len(restored.records) == 1 and len(path.read_text(encoding="utf-8").splitlines()) == 1
    changed = record()
    changed["response"]["choices"][0]["message"]["content"] = "changed"
    with pytest.raises(IntegrityError, match="conflicting_completion_id"):
        restored.add(changed)


def test_masks_distinguish_sampled_tokens_from_prompt_history_and_resume():
    first = record()
    second = record(completion(response_id="next"))
    second["response"]["choices"][0]["prompt_token_ids"] = [10, 11, 20, 21, 30]
    second["response"]["choices"][0]["token_ids"] = [40, 41]
    second["response"]["choices"][0]["logprobs"]["content"] = [{"token": "token_id:40", "logprob": -.3}, {"token": "token_id:41", "logprob": -.4}]
    second["response"]["usage"]["prompt_tokens"] = 5
    recorder = Recorder()
    for r in (first, second):
        recorder.add(r)
    audit = recorder.audit(trace([first, second]), sequence_tokens=7, expected_model_version="policy@0")
    assert audit["sequences"][0]["assistant_mask"] == [False, False, True, True, False, True, True]
    assert audit["assistant_tokens"] == 4 and audit["training_tokens"] == 7
    actual = SimpleNamespace(token_ids=audit["sequences"][0]["token_ids"], assistant_mask=audit["sequences"][0]["assistant_mask"],
                             logprobs=[float("nan"), float("nan"), -.1, -.2, float("nan"), -.3, -.4])
    compare_art_sequences(audit["sequences"], [actual])
    actual.assistant_mask = [True] * 7
    with pytest.raises(IntegrityError, match="art_token_or_mask_mismatch"):
        compare_art_sequences(audit["sequences"], [actual])
    with pytest.raises(IntegrityError, match="budget"):
        recorder.audit(trace([first, second]), sequence_tokens=6, expected_model_version="policy@0")


def test_malformed_tool_pairs_and_missing_or_mixed_behavior_are_invalid():
    data = record()
    recorder = Recorder()
    recorder.add(data)
    state = trace([data])
    state["state"]["messages"].append({"role": "assistant", "tool_calls": [{"id": "orphan"}]})
    state["state"]["model_calls"] = 2
    with pytest.raises(IntegrityError, match="missing_or_repeated"):
        recorder.audit(state, sequence_tokens=100, expected_model_version="policy@0")
    state = trace([data])
    state["state"]["messages"][-1]["tool_calls"] = [{"id": "orphan"}]
    with pytest.raises(ValueError, match="unpaired_tool_calls"):
        recorder.audit(state, sequence_tokens=100, expected_model_version="policy@0")
    with pytest.raises(IntegrityError, match="mixed_model"):
        recorder.audit(trace([data]), sequence_tokens=100, expected_model_version="different")


def group_records():
    return [{"scenario_id": "scenario", "initial_state_sha256": "state", "policy_version": 0,
             "identity": {"thread_id": "thread-" + str(i), "task_id": "task-" + str(i)},
             "completion_ids": ["completion-" + str(i)]} for i in range(2)]


@pytest.mark.parametrize("mutation,reason", [
    (lambda rows: rows.pop(), "invalid_group_size"),
    (lambda rows: rows[1].update(scenario_id="other"), "mixed_scenario_group"),
    (lambda rows: rows[1].update(initial_state_sha256="other"), "mixed_scenario_group"),
    (lambda rows: rows[1].update(policy_version=1), "mixed_policy_group"),
    (lambda rows: rows[1]["identity"].update(thread_id="thread-0"), "shared_trajectory_state"),
    (lambda rows: rows[1].update(completion_ids=["completion-0"]), "repeated_group_completion"),
])
def test_invalid_groups_never_mix_or_fill_from_other_scenarios(mutation, reason):
    rows = group_records()
    mutation(rows)
    with pytest.raises(IntegrityError, match=reason):
        verify_group(rows, size=2)


def test_config_and_check_do_not_import_art_or_torch():
    config = load_config("configs/training/smoke.yaml")
    checked = check(config)
    assert checked["locks"]["uv.lock"]["packages"] > 100
    assert checked["train_count"] == 400 and checked["known_v100_compatible"] is False
    args = backend_model_arguments(config)
    assert args["_internal_config"]["engine_args"]["max_model_len"] == 8192
    assert args["_internal_config"]["engine_args"]["max_num_seqs"] == 1
    assert config.experiment().model.max_tokens == 2048
    assert "torch" not in sys.modules and "art" not in sys.modules
    with pytest.raises(ValueError, match="separate Linux"):
        preflight(config)


class CompletionObject:
    def __init__(self, values):
        self.values = values
    def model_dump(self, **kwargs):
        return deepcopy(self.values)


class FakeARTModel:
    def __init__(self, source):
        self.source, self.requests, self.options = source, [], []
        self.client_factory_calls = 0
        self.chat = SimpleNamespace(completions=self)
    def openai_client(self):
        self.client_factory_calls += 1
        return self
    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self
    def get_inference_name(self):
        return "fixture-model"
    async def create(self, **kwargs):
        self.requests.append(kwargs)
        reply = await self.source.generate(kwargs["messages"])
        values = completion(reply.message.as_message(), response_id=uuid4().hex)
        values["choices"][0]["finish_reason"] = reply.finish_reason
        return CompletionObject(values)


def interfaces(counters):
    def wrapped(model, fn):
        counters.append("wrap_rollout")
        return fn
    def attach(recorder, trajectory):
        trajectory.records = deepcopy(recorder.records)
    return ARTInterfaces(wrapped, lambda **kwargs: SimpleNamespace(**kwargs),
        lambda rows, **kwargs: SimpleNamespace(trajectories=rows, **kwargs), attach)


def test_async_adapter_preserves_ids_arguments_usage_and_actual_budget():
    async def run():
        cfg = load_config("configs/training/smoke.yaml").experiment().model
        message = AssistantMessage.model_validate({"tool_calls": [{"id": "call-id", "type": "function",
            "function": {"name": "get_order", "arguments": '{"order_id":"ORD-1001"}'}}]})
        model = FakeARTModel(ModelClient(cfg.model_copy(update={"provider": "mock"}), mock_responses=[message]))
        recorder = Recorder()
        client = ARTModelClient(model, cfg, recorder, chat_factory=lambda bridge, **kwargs: bridge)
        reply = await client.generate([{"role": "user", "content": "hello"}], tools=(TOOL_CONTRACTS["get_order"],))
        assert reply.message.tool_calls[0].id == "call-id"
        assert reply.message.tool_calls[0].function.arguments == '{"order_id":"ORD-1001"}'
        assert reply.usage["completion_tokens"] == 2
        assert model.requests[0]["max_tokens"] == 2048 and model.requests[0]["logprobs"] is True
        assert model.requests[0]["extra_body"]["return_token_ids"] is True
        assert model.requests[0]["tools"][0]["function"]["name"] == "get_order"
        assert model.options[0]["max_retries"] == 0
        assert model.client_factory_calls == 1
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["query", "refund_rejected", "clarify_refund", "combined", "lookup", "unresolved"])
def test_shared_graph_group_isolation_approval_and_resume_with_real_mcp(tmp_path, mode):
    scenario = next(s for s in load_dataset("data/scenarios/v1", "train") if s.mode == mode)
    config = load_config("configs/training/smoke.yaml")
    models, wrappers = [], []
    def client(s, cfg, recorder):
        model = FakeARTModel(scripted_client(s, cfg.model_copy(update={"provider": "mock"})))
        models.append(model)
        return ARTModelClient(model, cfg, recorder, chat_factory=lambda bridge, **kwargs: bridge)
    group, report = asyncio.run(sample_group(None, scenario, config, policy_version=0, output=tmp_path,
        interfaces=interfaces(wrappers), client_factory=client))
    assert group is not None, report
    assert report["valid_size"] == 2 and report["rewards"] == [1, 1]
    assert wrappers == ["wrap_rollout", "wrap_rollout"]
    assert all(t.initial_policy_version == t.final_policy_version == 0 for t in group.trajectories)
    assert len({r["integrity"]["identity"]["thread_id"] for r in report["results"]}) == 2
    assert all(r["integrity"]["assistant_tokens"] == 2 * len(m.requests) for r, m in zip(report["results"], models))
    if mode == "clarify_refund":
        assert all(r["attempt"]["trajectory"]["state"]["input_receipts"] for r in report["results"])
    if mode == "refund_rejected":
        assert all(not r["attempt"]["trajectory"]["operations"] for r in report["results"])


def test_business_failure_can_train_but_missing_tokens_discard_entire_group(tmp_path):
    scenario = next(s for s in load_dataset("data/scenarios/v1", "train") if s.mode == "query")
    config = load_config("configs/training/smoke.yaml")
    calls = 0
    def factory(s, cfg, recorder):
        nonlocal calls
        calls += 1
        mock = cfg.model_copy(update={"provider": "mock"})
        source = scripted_client(s, mock) if calls % 2 else ModelClient(mock, mock_responses=[AssistantMessage(content="done")])
        return ARTModelClient(FakeARTModel(source), cfg, recorder, chat_factory=lambda b, **kwargs: b)
    group, report = asyncio.run(sample_group(None, scenario, config, policy_version=0, output=tmp_path / "mixed-rewards",
        interfaces=interfaces([]), client_factory=factory))
    assert group is not None and report["rewards"] == [1, 0] and report["zero_variance"] is False
    def no_tokens(s, cfg, recorder):
        # An application mock reply has no training token evidence; it must not be made trainable.
        # Keep the advertised training config while returning explicit scripted replies.
        source = scripted_client(s, cfg.model_copy(update={"provider": "mock"}))
        class NoTokens:
            config = cfg
            async def generate(self, messages, *, tools=()):
                return await source.generate(messages, tools=tools)
        return NoTokens()
    group, report = asyncio.run(sample_group(None, scenario, config, policy_version=0, output=tmp_path / "missing-tokens",
        interfaces=interfaces([]), client_factory=no_tokens))
    assert group is None and report["valid_size"] == 0 and report["failures"] == ["incomplete_group"]
    assert all(r["discard_reason"] for r in report["results"])


@pytest.mark.parametrize("fail_update", [False, True])
def test_training_dispatch_and_failure_report_use_existing_backend_contract(tmp_path, monkeypatch, fail_update):
    """Dispatch spy only; no actual ART import, model loading or parameter update."""
    import types
    import art_trainlab.train as training
    import os
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setenv("ART_VLLM_RUNTIME_BIN", "external-runtime-fixture")

    config = load_config("configs/training/smoke.yaml").model_copy(update={
        "output_dir": tmp_path, "steps": 1, "max_sampling_groups": 2})
    monkeypatch.setattr(training, "preflight", lambda c: {"source": "dispatch_fixture"})
    monkeypatch.setattr(training, "offline_environment", lambda: None)
    seen = {"closed": False, "train": [], "groups": 0}
    class Model:
        def __init__(self, **kwargs):
            assert kwargs["run_name"] and kwargs["base_model"].endswith("Qwen2.5-7B-Instruct")
        async def register(self, backend):
            seen["registered"] = True
        async def get_step(self):
            return 0
        async def log(self, *args, **kwargs):
            seen["logged"] = kwargs["step"]
    class Backend:
        def __init__(self, **kwargs):
            pass
        async def train(self, model, groups, **kwargs):
            seen["train"].append(kwargs)
            assert seen["groups"] == 2
            if fail_update:
                raise RuntimeError("fixture update failure")
            return SimpleNamespace(step=1, metrics={"loss": .5}, checkpoint_path="fixture-checkpoint")
        async def close(self):
            seen["closed"] = True
    packet = {"token_ids": [10, 20], "assistant_mask": [False, True], "logprobs": [None, -.1]}
    async def sample(*args, **kwargs):
        seen["groups"] += 1
        if seen["groups"] == 1:
            return None, {"zero_variance": False, "failures": ["incomplete_group"]}
        trajectories = [SimpleNamespace(messages_and_choices=[], tools=[], additional_histories=[]) for _ in range(2)]
        return SimpleNamespace(trajectories=trajectories), {"zero_variance": False, "failures": [],
            "results": [{"integrity": {"sequences": [packet]}} for _ in range(2)]}
    monkeypatch.setattr(training, "sample_group", sample)
    art = types.ModuleType("art")
    art.TrainableModel = Model
    local = types.ModuleType("art.local")
    local.LocalBackend = Backend
    trajectories = types.ModuleType("art.trajectories")
    trajectories.LegacyHistory = lambda **kwargs: SimpleNamespace(**kwargs)
    tokenize = types.ModuleType("art.preprocessing.tokenize")
    tokenize.assemble_vllm_training_sequences = lambda **kwargs: [SimpleNamespace(
        token_ids=[10, 20], assistant_mask=[0, 1], logprobs=[float("nan"), -.1])]
    transformers = types.ModuleType("transformers")
    transformers.AutoTokenizer = SimpleNamespace(from_pretrained=lambda *args, **kwargs: object())
    for name, module in {"art": art, "art.local": local, "art.trajectories": trajectories,
                         "art.preprocessing.tokenize": tokenize, "transformers": transformers}.items():
        monkeypatch.setitem(sys.modules, name, module)
    if fail_update:
        with pytest.raises(RuntimeError, match="fixture update failure"):
            asyncio.run(training.run(config))
    else:
        report = asyncio.run(training.run(config))
        assert report["status"] == "completed" and len(report["updates"]) == 1
        assert seen["logged"] == 1
    assert seen["closed"] is True
    assert seen["train"][0]["loss_fn"] == "cispo" and seen["train"][0]["allow_training_without_logprobs"] is False
    assert seen["train"][0]["packed_sequence_length"] == 8192 and seen["train"][0]["save_checkpoint"] is True
    saved = json.loads(next(tmp_path.glob("*/training.json")).read_text(encoding="utf-8"))
    assert len(saved["groups"]) == 2
    assert saved["status"] == ("failed" if fail_update else "completed")
    assert os.environ["ART_VLLM_RUNTIME_BIN"] == str(training.TRAINING_ROOT / "runtime/.venv/bin/art-vllm-runtime-server")
    assert os.environ["PATH"].split(os.pathsep)[0] == str(training.TRAINING_ROOT / "runtime/.venv/bin")


def test_linux_preflight_rejects_v100_before_backend_loading(tmp_path, monkeypatch):
    import types
    import art_trainlab.train as training
    from art_trainlab.config import ART_REVISION

    assets = tmp_path / "Qwen2.5-7B-Instruct"
    assets.mkdir()
    config = load_config("configs/training/smoke.yaml").model_copy(update={"base_model": assets, "base_revision": "0" * 40})
    monkeypatch.setattr(training.sys, "platform", "linux")
    monkeypatch.setattr(training.sys, "prefix", str(tmp_path / "training-env-fixture"))
    distribution = SimpleNamespace(version="0.5.20", read_text=lambda name: json.dumps({"vcs_info": {"commit_id": ART_REVISION}}))
    monkeypatch.setattr(training.importlib.metadata, "distribution", lambda name: distribution)
    versions = {"langchain-core": "1.6.9", "langgraph": "1.2.14", "langgraph-checkpoint-sqlite": "3.1.1", "mcp": "1.30.0", "pydantic": "2.13.5"}
    monkeypatch.setattr(training.importlib.metadata, "version", lambda name: versions[name])
    torch = types.ModuleType("torch")
    torch.cuda = SimpleNamespace(is_available=lambda: True, get_device_capability=lambda device: (7, 0))
    monkeypatch.setitem(sys.modules, "torch", torch)
    with pytest.raises(ValueError, match="V100 is unsupported"):
        preflight(config)


@pytest.mark.parametrize("applied", [False, True])
def test_linux_preflight_requires_and_records_checked_runtime_patch(tmp_path, monkeypatch, applied):
    """GPU identity fixture only; the runtime checker is exercised separately on CPU."""
    import types
    import art_trainlab.train as training
    from art_trainlab.config import ART_REVISION

    assets = tmp_path / "Qwen2.5-7B-Instruct"
    assets.mkdir()
    config = load_config("configs/training/smoke.yaml").model_copy(update={"base_model": assets, "base_revision": "0" * 40})
    monkeypatch.setattr(training.sys, "platform", "linux")
    monkeypatch.setattr(training.sys, "prefix", str(tmp_path / "training-env-fixture"))
    distribution = SimpleNamespace(version="0.5.20", read_text=lambda name: json.dumps({"vcs_info": {"commit_id": ART_REVISION}}))
    monkeypatch.setattr(training.importlib.metadata, "distribution", lambda name: distribution)
    versions = {"langchain-core": "1.6.9", "langgraph": "1.2.14", "langgraph-checkpoint-sqlite": "3.1.1", "mcp": "1.30.0", "pydantic": "2.13.5"}
    monkeypatch.setattr(training.importlib.metadata, "version", lambda name: versions[name])
    torch = types.ModuleType("torch")
    torch.__version__, torch.version = "fixture", SimpleNamespace(cuda="fixture")
    torch.cuda = SimpleNamespace(is_available=lambda: True, get_device_capability=lambda device: (8, 6),
                                get_device_name=lambda device: "GPU identity fixture")
    monkeypatch.setitem(sys.modules, "torch", torch)
    patch = {"applied": applied, "source_sha256": "fixture-patched-source"}
    details = {"versions": {"art-vllm-runtime": "0.1.0", "vllm": "0.25.1+cu129"},
               "revision": ART_REVISION, "compatibility_patch": patch}
    def checked(command, **kwargs):
        assert command == [str(training.TRAINING_ROOT / "runtime/.venv/bin/python"),
                           str(training.TRAINING_ROOT / "runtime/prepare.py"), "--check"]
        return json.dumps(details)
    monkeypatch.setattr(training.subprocess, "check_output", checked)
    if applied:
        assert preflight(config)["runtime"]["compatibility_patch"] == patch
    else:
        with pytest.raises(ValueError, match="Installed runtime differs"):
            preflight(config)
