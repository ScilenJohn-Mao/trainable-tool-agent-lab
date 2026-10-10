"""Claim queued tasks and resume recorded human replies on the shared agent graph."""

import os
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from apps.worker.scheduler import WorkerScheduler
from tool_agent_lab.agent.config import AgentConfig, load_agent_config
from tool_agent_lab.agent.graph import Agent
from tool_agent_lab.agent.model_client import AssistantMessage, ModelClient
from tool_agent_lab.agent.nodes import AgentNodes
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.runtime.leases import ExecutionLease, LeaseService
from tool_agent_lab.runtime.task_service import change_status, load_current_attempt
from tool_agent_lab.schemas.tasks import TaskStatus
from tool_agent_lab.settings import Settings
from tool_agent_lab.storage.checkpoints import open_checkpoints
from tool_agent_lab.storage.database import connect, initialize_database, transaction
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository


class WorkerBusy(RuntimeError):
    pass


@contextmanager
def worker_lock(database: Path) -> Iterator[None]:
    """Hold an OS lock for this application's sole worker; release it on exit."""
    path = database.resolve().with_name(database.name + ".worker.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            acquire = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as error:
            raise WorkerBusy(f"A worker already holds {path}") from error
        try:
            yield
        finally:
            stream.seek(0)
            release()


class WorkerRunner:
    def __init__(self, settings: Settings, model: ModelClient, config: AgentConfig, saver: AsyncSqliteSaver,
                 *, mock_responses: Sequence[AssistantMessage] = (), lease_seconds: float = 30.0,
                 heartbeat_interval: float = 10.0) -> None:
        self.settings = settings
        self.model = model
        self.config = config
        self.saver = saver
        self.mock_responses = tuple(mock_responses)
        self.rules = BusinessRules.from_file(settings.business_data_dir / "spec.json")
        self.scheduler = WorkerScheduler(LeaseService(
            settings.app_db_path, owner_id=settings.dev_owner_id, model_version=model.config.version,
            config_version=config.version, business_time=self.rules.business_time, lease_seconds=lease_seconds,
        ), heartbeat_interval=heartbeat_interval)

    def claim_next(self) -> ExecutionLease | None:
        return self.scheduler.leases.claim()

    def _agent(self, model_calls: int = 0) -> Agent:
        # Scripted replies restart at the saved decision offset; real clients retain their model cache.
        model = ModelClient(self.model.config, mock_responses=self.mock_responses[model_calls:]) if self.model.config.provider == "mock" else self.model
        nodes = AgentNodes(self.settings.app_db_path, self.settings.business_data_dir, self.rules, model, self.config)
        return Agent(nodes, checkpointer=self.saver)

    async def _ready_reply(self) -> tuple[Agent, str, str] | None:
        with connect(self.settings.app_db_path) as connection:
            rows = connection.execute(
                """SELECT t.task_id FROM tasks t JOIN attempts a ON a.attempt_id=t.current_attempt_id
                AND a.task_id=t.task_id AND a.owner_id=t.owner_id
                WHERE t.owner_id=? AND t.status='running' AND a.status='running'
                AND a.model_version=? AND a.config_version=? AND a.lease_worker_id IS NULL
                ORDER BY julianday(t.created_at), t.task_id""",
                (self.settings.dev_owner_id, self.model.config.version, self.config.version),
            ).fetchall()
        owner = self.settings.dev_owner_id
        for row in rows:
            agent = self._agent()
            _, binding = agent.binding(row["task_id"], owner)
            snapshot = await agent.graph.aget_state(binding)
            if not snapshot.values:
                continue
            state = snapshot.values
            with connect(self.settings.app_db_path) as connection:
                if snapshot.next == ("wait_approval",):
                    proposal = state["proposal"]
                    receipt = TaskRepository(connection).get_proposal_approval(
                        row["task_id"], proposal["proposal_id"], proposal["proposal_version"], owner)
                    request_id = receipt.request.request_id if receipt else None
                elif snapshot.next == ("wait_input",):
                    received = connection.execute(
                        """SELECT json_extract(payload_json,'$.request_id') FROM events
                        WHERE task_id=? AND attempt_id=? AND owner_id=? AND event_type='input_received'
                        AND json_extract(payload_json,'$.input_request_id')=? ORDER BY seq DESC LIMIT 1""",
                        (row["task_id"], state["identity"]["attempt_id"], owner, state["waiting"]["request_id"]),
                    ).fetchone()
                    request_id = received[0] if received else None
                else:
                    continue
            if request_id:
                return self._agent(state["model_calls"]), row["task_id"], request_id
        return None

    async def run_once(self) -> dict | None:
        """Run one compatible task until completion or the next human waiting point."""
        async with self.scheduler.slot:
            return await self._run_once()

    async def _run_once(self) -> dict | None:
        ready = await self._ready_reply()
        if ready:
            agent, task_id, request_id = ready
            lease = self.scheduler.leases.claim(task_id=task_id)
        else:
            lease = self.claim_next()
            if lease is None:
                return None
            agent, task_id, request_id = self._agent(), lease.attempt.task_id, None
        if lease is None:
            return None
        owner = self.settings.dev_owner_id

        async def invoke():
            try:
                return await agent.resume(task_id, request_id, owner_id=owner) if request_id else await agent.start(task_id, owner_id=owner)
            except Exception:
                # Execution exceptions propagate without automatic replay.
                with transaction(self.settings.app_db_path) as connection:
                    tasks = TaskRepository(connection)
                    _, attempt = load_current_attempt(tasks, task_id, owner)
                    change_status(tasks, EventRepository(connection), attempt, TaskStatus.FAILED, self.rules.business_time)
                raise

        state = await self.scheduler.execute(lease, invoke)
        interrupt = state.get("__interrupt__", ())
        return {"task_id": task_id, "thread_id": state["identity"]["thread_id"], "status": state["status"],
                "waiting": interrupt[0].value if interrupt else None, "result": state["result"]}


@asynccontextmanager
async def open_worker(settings: Settings, model: ModelClient, *, config: AgentConfig | None = None,
                      mock_responses: Sequence[AssistantMessage] = (), lease_seconds: float = 30.0,
                      heartbeat_interval: float = 10.0) -> AsyncIterator[WorkerRunner]:
    with worker_lock(settings.app_db_path):
        initialize_database(settings.app_db_path)
        async with open_checkpoints(settings.checkpoint_db_path, application_database=settings.app_db_path) as saver:
            yield WorkerRunner(settings, model, config or load_agent_config(), saver,
                               mock_responses=mock_responses, lease_seconds=lease_seconds,
                               heartbeat_interval=heartbeat_interval)
