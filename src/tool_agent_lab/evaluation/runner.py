"""Evaluate frozen scenarios through the shared graph and real business tools."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx
from mcp.shared.exceptions import McpError
from langgraph.errors import GraphRecursionError
from pydantic import ValidationError

from tool_agent_lab.agent.model_client import ModelClient
from tool_agent_lab.agent.nodes import AgentNodes
from tool_agent_lab.evaluation.datasets import file_hash, load_dataset
from tool_agent_lab.evaluation.report import build_report, write_report
from tool_agent_lab.evaluation.verifier import verdict, verify
from tool_agent_lab.runtime.task_service import TaskError
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.simulation.environment import open_environment
from tool_agent_lab.simulation.user_simulator import UserSimulator


class MeasuredModel:
    def __init__(self, client):
        self.client = client
        self.calls = []

    @property
    def config(self):
        return self.client.config

    @config.setter
    def config(self, value):
        self.client.config = value

    async def generate(self, messages, *, tools=()):
        started = perf_counter()
        record = {"usage": None, "error": None, "tool_calls": 0}
        self.calls.append(record)
        try:
            reply = await self.client.generate(messages, tools=tools)
            record.update(usage=reply.usage, response_id=reply.response_id,
                          response_model=reply.response_model, finish_reason=reply.finish_reason,
                          tool_calls=len(reply.message.tool_calls))
            return reply
        except Exception as error:
            record["error"] = type(error).__name__
            raise
        finally:
            record["seconds"] = perf_counter() - started


class MeasuredNodes(AgentNodes):
    def __post_init__(self):
        super().__post_init__()
        self.tool_seconds = 0.0

    async def execute(self, state):
        started = perf_counter()
        try:
            return await super().execute(state)
        finally:
            self.tool_seconds += perf_counter() - started

    def propose(self, state):
        started = perf_counter()
        try:
            return super().propose(state)
        finally:
            self.tool_seconds += perf_counter() - started


class MeasuredUser(UserSimulator):
    def __init__(self, behaviour):
        super().__init__(behaviour)
        self.waiting_started = None
        self.seconds = 0.0

    def request(self, waiting):
        request = super().request(waiting)
        if self.waiting_started is not None:
            self.seconds += perf_counter() - self.waiting_started
            self.waiting_started = None
        return request


def classify_exception(error: Exception, *, model_error: bool = False, scripted: bool = False) -> str:
    if isinstance(error, ExceptionGroup):
        categories = {classify_exception(e, model_error=model_error, scripted=scripted) for e in error.exceptions}
        return "infrastructure" if categories == {"infrastructure"} else "invalid" if "invalid" in categories else "business"
    if isinstance(error, (OSError, sqlite3.OperationalError, httpx.TransportError, McpError)):
        return "infrastructure"
    if isinstance(error, httpx.HTTPStatusError):
        return "infrastructure" if error.response.status_code in (408, 429) or error.response.status_code >= 500 else "invalid"
    if isinstance(error, (TaskError, GraphRecursionError)) or (model_error and not scripted and isinstance(error, (ValueError, ValidationError))):
        return "business"
    return "invalid"


async def run_attempt(scenario, experiment, client, *, root=None, keep_artifacts=False) -> dict:
    measured = MeasuredModel(client)
    nodes = None
    trace = None
    error_info = None
    artifact_dir = None
    user = MeasuredUser(scenario.user)
    started = perf_counter()

    def factory(*args):
        nonlocal nodes
        nodes = MeasuredNodes(*args)
        return nodes

    try:
        async with open_environment(scenario, experiment=experiment, model=measured, root=root,
                                    keep_artifacts=keep_artifacts, nodes_factory=factory) as environment:
            artifact_dir = str(environment.settings.runtime_dir) if keep_artifacts else None
            environment.user = user
            try:
                await environment.start()
                while environment.state.get("__interrupt__"):
                    # Automated users respond immediately; no fixed business-clock durations are used.
                    user.waiting_started = perf_counter()
                    if not await environment.respond():
                        break
            except Exception as error:
                environment.error = {"type": type(error).__name__, "message": str(error)}
                trace = await environment.export()
                raise
            trace = await environment.export()
            result = verify(scenario, trace, nodes.rules, nodes.policy_catalog)
    except Exception as error:
        error_info = {"type": type(error).__name__, "message": str(error)}
        category = classify_exception(error, model_error=bool(measured.calls and measured.calls[-1]["error"]),
                                      scripted=experiment.model.provider == "mock")
        result = verdict(category, ["exception:" + type(error).__name__])
    elapsed = perf_counter() - started
    model_seconds = sum(c["seconds"] for c in measured.calls)
    tool_seconds = nodes.tool_seconds if nodes else 0.0
    waiting_seconds = user.seconds
    tool_calls = sum(c["tool_calls"] for c in measured.calls)
    return {"evaluation_attempt_id": uuid4().hex, "verdict": result, "error": error_info,
            "artifact_dir": artifact_dir, "trajectory": trace, "effective_model": measured.config.model_dump(mode="json"),
            "observations": {"model_calls": measured.calls, "tool_calls": tool_calls,
                             "seconds": {"total": elapsed, "model": model_seconds, "tools": tool_seconds,
                                         "waiting": waiting_seconds,
                                         "orchestration": max(0.0, elapsed - model_seconds - tool_seconds - waiting_seconds)}}}


async def evaluate(dataset: str | Path, split: str, experiment, *, scenario_ids=(), limit=None,
                   infrastructure_retries=0, model_factory=None, output=None, root=None, keep_artifacts=False) -> dict:
    if not 0 <= infrastructure_retries <= 3:
        raise ValueError("infrastructure_retries must be between zero and three")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    dataset = (PROJECT_ROOT / dataset).resolve()
    scenarios = load_dataset(dataset, split)
    requested = set(scenario_ids)
    if requested - {s.scenario_id for s in scenarios}:
        raise ValueError("Unknown scenario IDs in selected split")
    selected = [s for s in scenarios if not requested or s.scenario_id in requested]
    selected = selected[:limit] if limit else selected
    if output is not None and Path(output).exists():
        raise FileExistsError("Choose a new report path; existing evaluations are preserved")
    shared_client = ModelClient(experiment.model) if model_factory is None else None
    if experiment.model.provider == "mock" and model_factory is None:
        raise ValueError("Mock evaluation requires explicitly supplied scenario replies")
    metadata = {"run_id": uuid4().hex, "started_at": datetime.now(UTC).isoformat(), "status": "running",
                "dataset": {"path": str(dataset), "split": split, "manifest_sha256": file_hash(dataset / "manifest.json"),
                            "manifest": json.loads((dataset / "manifest.json").read_text(encoding="utf-8")),
                            "split_count": len(scenarios), "selected_ids": [s.scenario_id for s in selected]},
                "experiment": experiment.model_dump(mode="json"),
                "measurement": "scripted_engineering_check" if experiment.model.provider == "mock" else "model_evaluation",
                "infrastructure_retries": infrastructure_retries,
                "timing_definition": {"total": "setup, graph, export and cleanup; includes failed attempts",
                                      "model": "generate wall time", "tools": "proposal and execute nodes including MCP startup",
                                      "waiting": "automatic user scheduling gaps", "orchestration": "remaining wall time"}}
    cases = [{"scenario_id": s.scenario_id, "root_scenario_id": s.scenario_id, "variant": "normal",
              "family_id": s.family_id, "task_type": s.task_type, "cohort": s.cohort,
              "fixture_version": s.fixture_version, "attempts": []} for s in selected]

    def save():
        report = build_report(metadata, cases)
        if output is not None:
            write_report(output, report)
        return report

    save()
    for scenario, case in zip(selected, cases, strict=True):
        for number in range(infrastructure_retries + 1):
            client = model_factory(scenario) if model_factory else shared_client
            attempt = await run_attempt(scenario, experiment, client, root=root, keep_artifacts=keep_artifacts)
            attempt["number"] = number + 1
            case["attempts"].append(attempt)
            save()
            if attempt["verdict"]["category"] != "infrastructure":
                break
    metadata.update(status="completed", finished_at=datetime.now(UTC).isoformat())
    return save()
