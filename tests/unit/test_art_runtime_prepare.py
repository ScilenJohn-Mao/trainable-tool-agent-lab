"""CPU regressions for pinned ART policy capture with synthetic runner batches."""

import ast
import builtins
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from training.art.runtime import prepare as runtime


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/art_runtime/policy_spans.py.txt"
SOURCE = FIXTURE.read_text(encoding="utf-8")
CONTEXT_FIELD = "_art_executing_policy_context"
IDENTITY = {"generation_id": "generation-3", "policy_version": 3, "lora_slot": "run:active", "update_seq": 3}


def capture_functions(*, patched=True):
    """Execute actual pinned capture/context functions without importing GPU libraries."""
    tree = ast.parse(runtime.rewrite(SOURCE) if patched else SOURCE)
    namespace = {"_EXECUTING_POLICY_CONTEXT_FIELD": CONTEXT_FIELD,
        "ART_POLICY_TOKEN_SPANS_FIELD": "art_policy_token_spans",
        "ART_PROMPT_POLICY_TOKEN_SPANS_FIELD": "art_prompt_policy_token_spans",
        "_CACHED_PROMPT_POLICY_SPAN_FIELD": "_art_cached_prompt_policy_span",
        "_WORKER_LORA_POLICY_BY_ID": {7: dict(IDENTITY)}}
    factories = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                 and node.name in {"make_execute_model", "make_sample_tokens", "make_get_output"}]
    tree.body.extend(factories)
    ast.fix_missing_locations(tree)
    exec(compile(tree, str(FIXTURE), "exec"), namespace)
    return namespace


def batch(request="request", *, computed=0, prompt=7):
    return SimpleNamespace(req_ids=[request], num_reqs=1, num_computed_tokens_np=[computed],
        prefill_len_np=[prompt], req_id_to_index={request: 0}, request_lora_mapping=[7],
        lora_id_to_lora_request={7: SimpleNamespace(lora_int_id=7)})


def scheduled(request="request", tokens=3):
    return SimpleNamespace(num_scheduled_tokens={request: tokens})


def test_unpatched_pinned_capture_reproduces_profile_request_keyerror():
    functions = capture_functions(patched=False)
    runner = SimpleNamespace(execute_model_state=SimpleNamespace(input_batch=batch("req_0_896e5d7f73fc777a")))
    execute = functions["make_execute_model"](lambda *a, **kw: None)
    with pytest.raises(KeyError, match="req_0_896e5d7f73fc777a"):
        execute(runner, scheduled("_dummy_req_0"), dummy_run=True, is_profile=True)


@pytest.mark.parametrize("positional", [False, True])
def test_dummy_capture_preserves_model_execution_and_clears_stale_policy(positional):
    functions = capture_functions()
    runner = SimpleNamespace(execute_model_state=SimpleNamespace(input_batch=batch("req_0_896e5d7f73fc777a")))
    setattr(runner, CONTEXT_FIELD, {"previous-request": dict(IDENTITY)})
    calls = []
    result = object()
    def original(self, *args, **kwargs):
        calls.append((args, kwargs))
        return result
    execute = functions["make_execute_model"](original)
    if positional:
        output = execute(runner, scheduled("_dummy_req_0"), None, True)
    else:
        output = execute(runner, scheduled("_dummy_req_0"), dummy_run=True, is_profile=True)
    assert output is result and len(calls) == 1
    assert not hasattr(runner, CONTEXT_FIELD)
    assert runner.execute_model_state.input_batch.req_ids == ["req_0_896e5d7f73fc777a"]


@pytest.mark.parametrize("tokens,computed,completion", [(3, 0, []), (1, 6, [21, 22])])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_capture_keeps_prompt_completion_and_execution_policy(tokens, computed, completion, asynchronous):
    functions = capture_functions()
    runner = SimpleNamespace(execute_model_state=SimpleNamespace(input_batch=batch(computed=computed)))
    execute = functions["make_execute_model"](lambda *a, **kw: None)
    execute(runner, scheduled(tokens=tokens), dummy_run=False)
    output = SimpleNamespace(req_ids=["request"], sampled_token_ids=[completion])
    # Updating the adapter before sample_tokens must not relabel the executed batch.
    functions["_WORKER_LORA_POLICY_BY_ID"][7] = dict(IDENTITY, policy_version=4, update_seq=4)
    if asynchronous:
        class AsyncOutput:
            pass
        AsyncOutput.get_output = functions["make_get_output"](lambda self: output)
        delayed = AsyncOutput()
        sample = functions["make_sample_tokens"](lambda *a, **kw: delayed)
        assert sample(runner) is delayed
        output = delayed.get_output()
    else:
        sample = functions["make_sample_tokens"](lambda *a, **kw: output)
        assert sample(runner) is output
    assert not hasattr(runner, CONTEXT_FIELD)
    prompt_spans = output.art_prompt_policy_token_spans["request"]
    assert all(span["policy_version"] == 3 for span in prompt_spans)
    assert prompt_spans[-1]["end_token"] == min(computed + tokens + 1, 7)
    if completion:
        assert output.art_policy_token_spans["request"] == [{"start_token": 0, "end_token": 2, **IDENTITY}]
    else:
        assert not hasattr(output, "art_policy_token_spans")


def test_real_request_with_dummy_like_id_is_still_tracked():
    functions = capture_functions()
    runner = SimpleNamespace(execute_model_state=SimpleNamespace(input_batch=batch("_dummy_req_0")))
    execute = functions["make_execute_model"](lambda *a, **kw: None)
    execute(runner, scheduled("_dummy_req_0"))
    assert getattr(runner, CONTEXT_FIELD)["_dummy_req_0"]["policy_version"] == 3


@pytest.mark.parametrize("failure", ["missing_request", "missing_scheduler", "inconsistent_batch", "negative_tokens"])
def test_real_policy_errors_remain_fatal(failure):
    functions = capture_functions()
    input_batch = batch()
    scheduler = scheduled()
    expected, message = RuntimeError, ""
    if failure == "missing_request":
        scheduler = scheduled("different-request")
        expected, message = KeyError, "request"
    elif failure == "missing_scheduler":
        scheduler = None
        message = "scheduler output is required"
    elif failure == "inconsistent_batch":
        input_batch.prefill_len_np = []
        message = "state is inconsistent"
    elif failure == "negative_tokens":
        scheduler = scheduled(tokens=-1)
        message = "counts are invalid"
    runner = SimpleNamespace(execute_model_state=SimpleNamespace(input_batch=input_batch))
    execute = functions["make_execute_model"](lambda *a, **kw: None)
    with pytest.raises(expected, match=message):
        execute(runner, scheduler)


def test_original_model_failure_is_not_suppressed_for_dummy_run():
    functions = capture_functions()
    def failed(*args, **kwargs):
        raise RuntimeError("original model failed")
    with pytest.raises(RuntimeError, match="original model failed"):
        functions["make_execute_model"](failed)(SimpleNamespace(), scheduled(), dummy_run=True)


@pytest.fixture
def installed(tmp_path, monkeypatch):
    prefix = tmp_path / "runtime-env"
    path = prefix / "lib/art_vllm_runtime/policy_spans.py"
    path.parent.mkdir(parents=True)
    path.write_text(SOURCE, encoding="utf-8", newline="\n")
    distribution = SimpleNamespace(version="0.1.0",
        read_text=lambda name: json.dumps({"vcs_info": {"commit_id": runtime.ART_REVISION}}),
        locate_file=lambda relative: path)
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.sys, "prefix", str(prefix))
    monkeypatch.setattr(runtime.importlib.metadata, "distribution", lambda name: distribution)
    monkeypatch.setattr(runtime.importlib.metadata, "version", lambda name: "0.25.1+cu129")
    # Installed distribution fixture uses exact upstream function excerpts, not a full wheel.
    monkeypatch.setattr(runtime, "SOURCE_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    return path, distribution


def test_prepare_is_idempotent_and_does_not_modify_shared_cache_inode(installed, tmp_path):
    path, _ = installed
    cache = tmp_path / "cached-source.py"
    os.link(path, cache)
    before = path.read_bytes()
    assert runtime.prepare()["compatibility_patch"]["applied"] is False
    first = runtime.prepare(apply=True)["compatibility_patch"]
    modified = path.stat().st_mtime_ns
    second = runtime.prepare(apply=True)["compatibility_patch"]
    assert first["changed"] and first["applied"]
    assert second["applied"] and not second["changed"]
    assert path.stat().st_mtime_ns == modified
    assert cache.read_bytes() == before
    assert second["source_sha256"] == second["patched_sha256"]
    assert first["source_sha256"] == second["source_sha256"]


@pytest.mark.parametrize("alteration", ["extra_edit", "wrong_revision", "wrong_version", "wrong_vllm", "outside_environment"])
def test_prepare_rejects_unverified_runtime_without_writing(installed, tmp_path, monkeypatch, alteration):
    path, distribution = installed
    if alteration == "extra_edit":
        path.write_text(SOURCE + "\n# extra change\n", encoding="utf-8", newline="\n")
    elif alteration == "wrong_revision":
        distribution.read_text = lambda name: json.dumps({"vcs_info": {"commit_id": "0" * 40}})
    elif alteration == "wrong_version":
        distribution.version = "0.2.0"
    elif alteration == "wrong_vllm":
        monkeypatch.setattr(runtime.importlib.metadata, "version", lambda name: "0.25.2")
    elif alteration == "outside_environment":
        monkeypatch.setattr(runtime.sys, "prefix", str(tmp_path / "other-env"))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        runtime.prepare(apply=True)
    assert path.read_bytes() == before


def test_prepare_rejects_extra_changes_after_patch(installed):
    path, _ = installed
    runtime.prepare(apply=True)
    path.write_bytes(path.read_bytes() + b"\n# additional edit\n")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="differs beyond"):
        runtime.prepare(apply=True)
    assert path.read_bytes() == before


def test_prepare_cli_check_requires_patch_and_never_imports_gpu_libraries(installed, monkeypatch, capsys):
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"torch", "vllm", "art", "art_vllm_runtime"}:
            raise AssertionError("Preparation must not import GPU libraries")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert runtime.main(["--check"]) == 1
    assert "--apply" in capsys.readouterr().err
    assert runtime.main(["--apply"]) == 0
    capsys.readouterr()
    assert runtime.main(["--check"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["revision"] == runtime.ART_REVISION
    assert report["compatibility_patch"]["applied"] is True
