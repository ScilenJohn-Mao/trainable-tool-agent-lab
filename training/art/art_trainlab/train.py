"""Local checks and explicit Linux ART training through the existing backend API."""

import argparse
import asyncio
from datetime import UTC, datetime
import importlib.metadata
import json
from pathlib import Path
import random
import subprocess
import sys
import tomllib
from uuid import uuid4

from art_trainlab.config import ART_REVISION, ART_VERSION, TrainingConfig, load_config, offline_environment
from art_trainlab.recorder import compare_art_sequences
from art_trainlab.rollout import sample_group
from tool_agent_lab.evaluation.datasets import file_hash, load_dataset, validate_frozen
from tool_agent_lab.evaluation.report import write_report
from tool_agent_lab.settings import PROJECT_ROOT

TRAINING_ROOT = Path(__file__).resolve().parents[1]


def check(config):
    manifest = validate_frozen(config.dataset)
    locks = {}
    for relative in ("uv.lock", "runtime/uv.lock"):
        path = TRAINING_ROOT / relative
        lock = tomllib.loads(path.read_text(encoding="utf-8"))
        git_packages = [p for p in lock["package"] if "git" in p["source"]]
        if not git_packages or any(ART_REVISION not in p["source"]["git"] for p in git_packages):
            raise ValueError("Lock differs from fixed ART revision")
        locks[relative] = {"sha256": file_hash(path), "packages": len(lock["package"])}
    pending = ["Linux environment installation", "actual ART token/mask and GPU update verification"]
    if config.base_revision is None:
        pending.append("verified base-model revision")
    if not config.base_model.is_dir():
        pending.append("local Qwen2.5-7B-Instruct assets")
    return {"config": config.model_dump(mode="json"), "locks": locks,
            "dataset_manifest_sha256": file_hash(config.dataset / "manifest.json"),
            "train_count": manifest["splits"]["train"]["count"], "pending": pending,
            "minimum_gpu_compute_capability": "7.5", "known_v100_compatible": False}


def preflight(config):
    if sys.platform != "linux" or Path(sys.prefix).resolve() == (PROJECT_ROOT / ".venv").resolve():
        raise ValueError("Use the separate Linux training environment")
    if config.base_revision is None or not config.base_model.is_dir():
        raise ValueError("Prepare local 7B assets and provide their verified revision")
    distribution = importlib.metadata.distribution("openpipe-art")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    if distribution.version != ART_VERSION or direct.get("vcs_info", {}).get("commit_id") != ART_REVISION:
        raise ValueError("Installed ART version/revision differs from lock")
    for name, version in {"langchain-core": "1.6.9", "langgraph": "1.2.14", "langgraph-checkpoint-sqlite": "3.1.1",
                          "mcp": "1.30.0", "pydantic": "2.13.5"}.items():
        if importlib.metadata.version(name) != version:
            raise ValueError("Shared application dependency mismatch: " + name)
    import torch

    if not torch.cuda.is_available() or torch.cuda.get_device_capability(0) < (7, 5):
        raise ValueError("Fixed vLLM runtime requires CUDA GPU capability >=7.5; V100 is unsupported")
    runtime_python = TRAINING_ROOT / "runtime/.venv/bin/python"
    details = subprocess.check_output([str(runtime_python), "-c",
        "import importlib.metadata as m,json;d=m.distribution('art-vllm-runtime');"
        "print(json.dumps({'versions':{k:m.version(k) for k in ['art-vllm-runtime','vllm']},"
        "'revision':json.loads(d.read_text('direct_url.json') or '{}').get('vcs_info',{}).get('commit_id')}))"], text=True)
    runtime = json.loads(details)
    if runtime != {"versions": {"art-vllm-runtime": config.runtime_version, "vllm": config.vllm_version}, "revision": ART_REVISION}:
        raise ValueError("Installed runtime differs from fixed configuration")
    return {"gpu": torch.cuda.get_device_name(0), "cuda": torch.version.cuda, "torch": torch.__version__,
            "capability": torch.cuda.get_device_capability(0), "runtime": runtime}


def backend_model_arguments(config, *, run_name=None):
    return {"name": config.name, "run_name": run_name or config.name, "project": "tool-agent-lab", "base_model": str(config.base_model),
            "report_metrics": [], "base_path": str(config.output_dir / "backend"),
            "lora_config": {"rank": config.lora_rank, "alpha": config.lora_alpha, "dropout": 0,
                            "max_seq_length": config.sequence_tokens, "use_gradient_checkpointing": "unsloth"},
            "_internal_config": {
                "init_args": {"max_seq_length": config.sequence_tokens, "dtype": "float16", "load_in_4bit": True},
                "engine_args": {"dtype": "float16", "max_model_len": config.sequence_tokens,
                                "gpu_memory_utilization": config.gpu_memory_utilization, "max_num_seqs": config.active_generation},
                "vllm_runtime": {"mode": "managed"}}}


async def run(config):
    offline_environment()
    metadata = check(config)
    metadata["platform"] = preflight(config)
    import os
    import art
    from art.local import LocalBackend
    from art.preprocessing.tokenize import assemble_vllm_training_sequences
    from art.trajectories import LegacyHistory
    from transformers import AutoTokenizer

    os.environ["ART_VLLM_RUNTIME_PROJECT_ROOT"] = str(TRAINING_ROOT / "runtime")
    tokenizer = AutoTokenizer.from_pretrained(str(config.base_model), local_files_only=True)
    output = config.output_dir / (config.name + "-" + uuid4().hex)
    output.mkdir(parents=True)
    report = {"format_version": "art-training-v1", "run_id": output.name, "started_at": datetime.now(UTC).isoformat(),
              "metadata": metadata, "status": "running", "groups": [], "updates": [], "error": None}
    backend = LocalBackend(path=str(config.output_dir / "backend"))
    model = art.TrainableModel(**backend_model_arguments(config, run_name=output.name))
    try:
        await model.register(backend)
        scenarios = list(load_dataset(config.dataset, "train"))
        random.Random(config.seed).shuffle(scenarios)
        for index in range(config.max_sampling_groups):
            step = await model.get_step()
            group, record = await sample_group(model, scenarios[index % len(scenarios)], config,
                                               policy_version=step, output=output / "rollouts")
            report["groups"].append(record)
            write_report(output / "training.json", report)
            if group is None or record["zero_variance"]:
                continue
            for trajectory, result in zip(group.trajectories, record["results"], strict=True):
                histories = [LegacyHistory(messages_and_choices=trajectory.messages_and_choices, tools=trajectory.tools),
                             *trajectory.additional_histories]
                actual = assemble_vllm_training_sequences(tokenizer=tokenizer, histories=histories, advantage=1.0,
                    allow_training_without_logprobs=False, trajectory=trajectory)
                compare_art_sequences(result["integrity"]["sequences"], actual)
            update = await backend.train(model, [group], learning_rate=config.learning_rate, loss_fn=config.loss_fn,
                allow_training_without_logprobs=False, packed_sequence_length=config.sequence_tokens,
                logprob_calculation_chunk_size=8, save_checkpoint=True, optimizer_save_interval=1)
            if update.step <= step:
                raise ValueError("ART did not advance the training step")
            report["updates"].append({"step": update.step, "metrics": update.metrics,
                                      "checkpoint_path": str(update.checkpoint_path)})
            await model.log([group], metrics=update.metrics, step=update.step, split="train")
            write_report(output / "training.json", report)
            if len(report["updates"]) == config.steps:
                break
        report["status"] = "completed" if len(report["updates"]) == config.steps else "incomplete_updates"
    except Exception as error:
        report.update(status="failed", error={"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        write_report(output / "training.json", report)
        await backend.close()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/training/smoke.yaml")
    parser.add_argument("--base-revision", help="Verified 40-character revision of the already downloaded 7B base")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="Validate configuration, frozen data and locks without ART/torch imports")
    action.add_argument("--train", action="store_true", help="Explicitly run GPU sampling and updates in the Linux training environment")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.base_revision:
        config = TrainingConfig.model_validate(config.model_dump() | {"base_revision": args.base_revision})
    report = check(config) if args.check else asyncio.run(run(config))
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if args.check or report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
