"""Run frozen after-sales evaluations and save per-attempt evidence and metrics."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from scripts.evaluation_fixtures import scripted_client
from tool_agent_lab.agent.config import load_agent_config
from tool_agent_lab.agent.model_client import AssistantMessage, ModelClient, ModelConfig, load_model_config
from tool_agent_lab.evaluation.runner import evaluate
from tool_agent_lab.schemas.scenarios import ExperimentConfig


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/scenarios/v1"))
    parser.add_argument("--split", choices=("train", "validation", "test"), default="validation")
    parser.add_argument("--scenario", action="append", default=[], help="Exact scenario ID; repeat to select multiple")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--model-config", default="configs/models/mock.yaml")
    parser.add_argument("--agent-config", default="configs/agents/default.yaml")
    mocks = parser.add_mutually_exclusive_group()
    mocks.add_argument("--scripted-demo", action="store_true", help="Use scenario-aware fixtures; not autonomous model performance")
    mocks.add_argument("--mock-responses", type=Path, help="JSON mapping scenario IDs to explicit assistant messages")
    parser.add_argument("--infrastructure-retries", type=int, default=0)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument("--output", type=Path, required=True, help="New JSON report path; existing reports are never replaced")
    args = parser.parse_args(argv)
    config = ModelConfig(provider="mock", name="evaluation-fixtures", version="evaluation-fixtures-v1") if args.scripted_demo else load_model_config(args.model_config)
    if args.mock_responses and config.provider != "mock":
        parser.error("--mock-responses requires provider=mock")
    factory = None
    if args.scripted_demo:
        factory = lambda scenario: scripted_client(scenario, config)
    elif args.mock_responses:
        responses = {key: tuple(AssistantMessage.model_validate(m) for m in messages)
                     for key, messages in json.loads(args.mock_responses.read_text(encoding="utf-8")).items()}
        factory = lambda scenario: ModelClient(config, mock_responses=responses.get(scenario.scenario_id, ()))
    report = asyncio.run(evaluate(args.dataset, args.split, ExperimentConfig(model=config, agent=load_agent_config(args.agent_config)),
        scenario_ids=args.scenario, limit=args.limit, infrastructure_retries=args.infrastructure_retries,
        model_factory=factory, output=args.output, root=args.runtime_dir, keep_artifacts=args.keep_artifacts))
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"report": str(args.output.resolve()), "run_id": report["run_id"],
                      "measurement": report["measurement"], "metrics": report["metrics"]}, ensure_ascii=False, indent=2))
    return 0 if report["metrics"]["overall"]["successful"] == report["metrics"]["overall"]["planned"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
