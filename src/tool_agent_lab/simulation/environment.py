"""Run each trajectory in fresh SQLite databases using the shared agent graph."""

import asyncio
import json
import shutil
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from scripts.seed_demo import seed_demo
from tool_agent_lab.agent.config import load_agent_config
from tool_agent_lab.agent.graph import Agent
from tool_agent_lab.agent.model_client import AssistantMessage, ModelClient, load_model_config
from tool_agent_lab.agent.nodes import AgentNodes
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.runtime.task_service import TaskService
from tool_agent_lab.schemas.scenarios import ExperimentConfig, Scenario
from tool_agent_lab.schemas.tasks import Task
from tool_agent_lab.settings import PROJECT_ROOT, Settings, load_settings
from tool_agent_lab.simulation.initial_state import seed_initial_state
from tool_agent_lab.simulation.user_simulator import UserSimulator
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.checkpoints import open_checkpoints
from tool_agent_lab.storage.database import connect
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository


class SimulationEnvironment:
    def __init__(self, scenario: Scenario, settings: Settings, experiment: ExperimentConfig,
                 agent: Agent, task: Task) -> None:
        self.scenario = scenario
        self.settings = settings
        self.experiment = experiment
        self.agent = agent
        self.task = task
        self.user = UserSimulator(scenario.user)
        self.state: dict | None = None
        self.error: dict | None = None
        self.initial_business = self.business_snapshot()

    def business_snapshot(self) -> dict:
        """Read authoritative balances, ledger and consumed confirmation snapshots."""
        with connect(self.settings.app_db_path) as connection:
            business, tasks = BusinessRepository(connection), TaskRepository(connection)
            orders = [business.get_order(row[0], self.task.owner_id).model_dump(mode="json")
                      for row in connection.execute("SELECT order_id FROM orders WHERE owner_id=? ORDER BY order_id",
                                                    (self.task.owner_id,))]
            ledger = [business.get_operation(row[0], self.task.owner_id).model_dump(mode="json")
                      for row in connection.execute("SELECT operation_key FROM operations WHERE owner_id=? ORDER BY operation_key",
                                                    (self.task.owner_id,))]
            approvals = []
            for row in connection.execute(
                "SELECT p.task_id,a.request_id FROM approvals a JOIN proposals p USING(proposal_id,proposal_version) "
                "WHERE a.owner_id=? ORDER BY a.rowid", (self.task.owner_id,),
            ):
                approval = tasks.get_approval(row[0], row[1], self.task.owner_id)
                consumed = tasks.approval_consumed_at(row[0], row[1], self.task.owner_id)
                approvals.append(approval.model_dump(mode="json") | {
                    "consumed_at": consumed.isoformat() if consumed else None,
                })
        return {"orders": orders, "ledger": ledger, "approvals": approvals}

    async def start(self) -> dict:
        self.state = await self.agent.start(self.task.task_id, owner_id=self.task.owner_id)
        return self.state

    async def respond(self) -> bool:
        if self.state is None:
            raise ValueError("Start the trajectory before responding")
        interrupts = self.state.get("__interrupt__", ())
        if not interrupts:
            return False
        state = await self.user.respond(self.agent, self.task, interrupts[0].value)
        if state is None:
            return False
        self.state = state
        return True

    async def run(self) -> dict:
        """Drive human waiting points; model/tool decisions stay in the existing graph."""
        if self.state is None:
            await self.start()
        while await self.respond():
            pass
        return await self.export()

    async def export(self) -> dict:
        _, binding = self.agent.binding(self.task.task_id, self.task.owner_id)
        snapshot = await self.agent.graph.aget_state(binding)
        with connect(self.settings.app_db_path) as connection:
            business = BusinessRepository(connection)
            order_ids = [row[0] for row in connection.execute(
                "SELECT order_id FROM orders WHERE owner_id=? ORDER BY order_id", (self.task.owner_id,),
            )]
            orders = [business.get_order(order_id, self.task.owner_id).model_dump(mode="json") for order_id in order_ids]
            operation_keys = [row[0] for row in connection.execute(
                "SELECT operation_key FROM operations WHERE task_id=? AND attempt_id=? ORDER BY operation_key",
                (self.task.task_id, self.task.current_attempt_id),
            )]
            operations = [business.get_operation(key, self.task.owner_id).model_dump(mode="json") for key in operation_keys]
            events, cursor = [], 0
            while batch := EventRepository(connection).list_events(self.task.task_id, self.task.owner_id, after_seq=cursor):
                events.extend(event.model_dump(mode="json") for event in batch)
                cursor = batch[-1].seq
        task = TaskService(self.settings.app_db_path, business_time=self.scenario.business_time).get(
            self.task.task_id, owner_id=self.task.owner_id,
        )
        report = {
            "scenario": self.scenario.model_dump(mode="json"), "experiment": self.experiment.model_dump(mode="json"),
            "task": task.model_dump(mode="json"), "state": snapshot.values,
            "stop_reason": "user_script_exhausted" if task.status in ("waiting_input", "waiting_approval") else task.status.value,
            "orders": orders, "operations": operations, "events": events, "error": self.error,
            "business": self.business_snapshot(), "initial_business": self.initial_business,
        }
        (self.settings.log_dir / "trajectory.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8",
        )
        return report


@asynccontextmanager
async def open_environment(
    scenario: Scenario, *, settings: Settings | None = None, experiment: ExperimentConfig | None = None,
    model: ModelClient | None = None, mock_responses: Sequence[AssistantMessage] = (),
    root: str | Path | None = None, keep_artifacts: bool = False,
    nodes_factory=AgentNodes,
) -> AsyncIterator[SimulationEnvironment]:
    """A new context resets a scenario; only its newly created directory is removed."""
    source = settings or load_settings()
    if scenario.initial_state is not None:
        source = source.model_copy(update={"business_data_dir": (PROJECT_ROOT / scenario.initial_state.data_dir).resolve()})
    experiment = experiment or ExperimentConfig(model=load_model_config(), agent=load_agent_config())
    spec = json.loads((source.business_data_dir / "spec.json").read_text(encoding="utf-8"))
    rules = BusinessRules.from_file(source.business_data_dir / "spec.json")
    if (scenario.fixture_version, scenario.business_time) != (spec["version"], rules.business_time):
        raise ValueError("Scenario fixture version or business clock does not match")
    if model is not None and model.config != experiment.model:
        raise ValueError("Model client does not match the experiment configuration")
    parent = Path(root if root is not None else source.runtime_dir / "simulations").resolve()
    parent.mkdir(parents=True, exist_ok=True)
    directory = parent / f"trajectory-{uuid4().hex}"
    directory.mkdir()
    runtime = source.model_copy(update={"runtime_dir": directory, "dev_owner_id": scenario.owner_id})
    try:
        runtime.prepare_runtime_dirs()
        # The seed entry point rejects runtime paths; pass the source settings for its safety boundary.
        if scenario.initial_state is None:
            seed_demo(runtime.app_db_path, settings=source)
        else:
            seed_initial_state(runtime.app_db_path, scenario.initial_state, rules)
        task = TaskService(runtime.app_db_path, business_time=rules.business_time,
                           model_version=experiment.model.version, config_version=experiment.agent.version).create(
            scenario.request, owner_id=scenario.owner_id,
        )
        async with open_checkpoints(runtime.checkpoint_db_path, application_database=runtime.app_db_path) as saver:
            agent = Agent(nodes_factory(runtime.app_db_path, runtime.business_data_dir, rules,
                                    model or ModelClient(experiment.model, mock_responses=mock_responses), experiment.agent),
                          checkpointer=saver)
            environment = SimulationEnvironment(scenario, runtime, experiment, agent, task)
            try:
                yield environment
            except Exception as error:
                environment.error = {"type": type(error).__name__, "message": str(error)}
                raise
            finally:
                await environment.export()
    finally:
        if not keep_artifacts:
            if directory.resolve().parent != parent or directory.is_symlink() or directory.is_junction():
                raise ValueError("Trajectory cleanup boundary changed")
            # Windows directory observers can briefly retain a handle after files are closed.
            for attempt in range(5):
                try:
                    shutil.rmtree(directory)
                    break
                except OSError as error:
                    if getattr(error, "winerror", None) != 32 or attempt == 4:
                        raise
                    await asyncio.sleep(0.1)
