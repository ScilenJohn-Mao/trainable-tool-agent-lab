"""Run the independent local agent worker using shared model and task contracts."""

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

from pydantic import TypeAdapter

from apps.worker.runner import open_worker
from tool_agent_lab.agent.config import load_agent_config
from tool_agent_lab.agent.model_client import AssistantMessage, ModelClient, load_model_config
from tool_agent_lab.settings import PROJECT_ROOT, load_settings


async def run(args: argparse.Namespace) -> None:
    settings = load_settings(args.app_config)
    model = ModelClient(load_model_config(args.model_config))
    config = load_agent_config(args.agent_config)
    replies = ()
    if model.config.provider == "mock":
        if args.mock_responses is None:
            raise ValueError("Mock workers require --mock-responses with an explicit assistant-message JSON array")
        replies = TypeAdapter(tuple[AssistantMessage, ...]).validate_json(
            (PROJECT_ROOT / args.mock_responses).read_text(encoding="utf-8"))
    async with open_worker(settings, model, config=config, mock_responses=replies) as worker:
        while True:
            outcome = await worker.run_once()
            if outcome is not None or args.once:
                print(json.dumps(outcome or {"status": "idle"}, ensure_ascii=False), flush=True)
            if args.once:
                return
            if outcome is None:
                await asyncio.sleep(args.poll_interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-config", type=Path, help="Application YAML; default configs/app.yaml")
    parser.add_argument("--model-config", type=Path, default=Path("configs/models/mock.yaml"))
    parser.add_argument("--agent-config", type=Path, default=Path("configs/agents/default.yaml"))
    parser.add_argument("--mock-responses", type=Path, help="Explicit assistant-message JSON array, replayed per task")
    parser.add_argument("--once", action="store_true", help="Process one ready task or report idle and exit")
    parser.add_argument("--poll-interval", type=float, default=0.5)
    args = parser.parse_args(argv)
    if not math.isfinite(args.poll_interval) or args.poll_interval <= 0:
        parser.error("--poll-interval must be finite and positive")
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
