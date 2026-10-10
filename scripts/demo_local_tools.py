"""Run an offline local model through a read-only MCP tool/result conversation."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import sys
import time
from pathlib import Path

from tool_agent_lab.agent.model_client import ModelClient, ModelConfig, load_model_config
from tool_agent_lab.settings import PROJECT_ROOT, load_settings
from tool_agent_lab.tools.client import local_server_parameters, open_tool_session
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS

if __package__:
    from .seed_demo import seed_demo
else:
    from seed_demo import seed_demo


QUERY = "查询 ORD-1001 的实付金额和商品名称。只查询，不退款、不补偿、不转人工。先查订单，收到工具结果后用一句中文回答。"


async def readonly_roundtrip(model, tools, *, gpu=None, max_turns=3) -> dict:
    messages = [{"role": "user", "content": QUERY}]
    report = {"query": QUERY, "turns": [], "tool_results": [], "messages": messages}
    try:
        for turn in range(max_turns):
            if gpu is not None:
                gpu.synchronize()
                gpu.reset_peak_memory_stats()
            started = time.perf_counter()
            reply = await model.generate(messages, tools=tuple(TOOL_CONTRACTS.values()))
            if gpu is not None:
                gpu.synchronize()
            elapsed = time.perf_counter() - started
            profile = {"elapsed_seconds": elapsed, "includes_model_load": turn == 0,
                       "completion_tokens_per_elapsed_second": (
                           reply.usage["completion_tokens"] / elapsed if reply.usage else None)}
            if gpu is not None:
                free, total = gpu.mem_get_info()
                profile |= {"peak_allocated_mib": gpu.max_memory_allocated() / 2**20,
                            "peak_reserved_mib": gpu.max_memory_reserved() / 2**20,
                            "device_used_mib_after_request": (total - free) / 2**20}
            report["turns"].append({"reply": reply.model_dump(mode="json"), "profile": profile})
            messages.append(reply.message.as_message())
            if reply.finish_reason in ("timeout", "length"):
                return report | {"finish_reason": reply.finish_reason}
            if not reply.message.tool_calls:
                return report | {"finish_reason": "answered", "answer": reply.message.content,
                                 "tool_result_roundtrip": bool(report["tool_results"])}
            for call in reply.message.tool_calls:
                contract = TOOL_CONTRACTS[call.function.name]
                if not contract.read_only:
                    return report | {"finish_reason": "write_tool_not_allowed"}
                arguments = contract.argument_model.model_validate_json(call.function.arguments)
                tool_reply = await tools.call_tool(call.function.name, arguments.model_dump(mode="json"),
                                                   call_id=call.id)
                result = tool_reply.result.model_dump(mode="json")
                report["tool_results"].append({"name": call.function.name,
                                               "arguments": json.loads(call.function.arguments), "result": result})
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(result, ensure_ascii=False)})
                if result["status"] != "ok":
                    return report | {"finish_reason": "tool_error"}
        return report | {"finish_reason": "turn_budget_exhausted"}
    except Exception as error:
        # Preserve partial output from a failed real run, without retries or fallback.
        return report | {"finish_reason": "error", "error": {"type": type(error).__name__, "message": str(error)}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/models/qwen3b-local.yaml")
    parser.add_argument("--runtime-dir", type=Path, default=Path("artifacts/runtime/local-tool-demo"))
    parser.add_argument("--context-tokens", type=int, default=4096)
    parser.add_argument("--max-tokens", type=int, default=128)
    args = parser.parse_args(argv)
    config = load_model_config(args.config)
    if config.provider != "local" or not config.device.startswith("cuda:"):
        parser.error("This demo requires the local CUDA provider in the inference environment")
    config = ModelConfig.model_validate(config.model_dump() | {
        "context_tokens": args.context_tokens, "max_tokens": args.max_tokens,
    })
    # These flags affect only this process; the loader also requires local files.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch

    torch.cuda.set_device(config.device)
    runtime = (PROJECT_ROOT / args.runtime_dir).resolve()
    settings = load_settings()
    database = runtime / "app.sqlite3"
    seed = seed_demo(database, settings=settings)
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    async def run():
        async with open_tool_session(local_server_parameters(
            database=database, data_dir=settings.business_data_dir, owner_id=settings.dev_owner_id,
        )) as tools:
            return await readonly_roundtrip(ModelClient(config), tools, gpu=torch.cuda)

    report = asyncio.run(run())
    report |= {"model": config.model_dump(mode="json"), "seed": seed,
               "environment": {"python": sys.version, "platform": sys.platform,
                               "device": torch.cuda.get_device_name(),
                               "device_total_mib": torch.cuda.get_device_properties(config.device).total_memory / 2**20,
                               "packages": {name: importlib.metadata.version(name) for name in (
                                   "torch", "transformers", "peft", "accelerate", "bitsandbytes")}},
               "model_files": {name: hashlib.sha256((config.base_path / name).read_bytes()).hexdigest()
                               for name in ("config.json", "tokenizer_config.json", "model.safetensors.index.json")},
               "verified_huggingface_revision": None,
               "database_sha256_before": before,
               "database_sha256_after": hashlib.sha256(database.read_bytes()).hexdigest()}
    report["database_unchanged"] = report["database_sha256_before"] == report["database_sha256_after"]
    output = runtime / "receipt.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"receipt": str(output), "finish_reason": report["finish_reason"],
                      "tool_result_roundtrip": report.get("tool_result_roundtrip", False),
                      "database_unchanged": report["database_unchanged"], "answer": report.get("answer")},
                     ensure_ascii=False, indent=2))
    return 0 if report.get("tool_result_roundtrip") and report["database_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
