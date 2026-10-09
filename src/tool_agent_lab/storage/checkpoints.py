"""Open a persistent graph saver separate from the application database."""

from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from tool_agent_lab.settings import PROJECT_ROOT


@asynccontextmanager
async def open_checkpoints(path: str | Path, *, application_database: str | Path):
    database = (PROJECT_ROOT / path).resolve()
    if database == (PROJECT_ROOT / application_database).resolve():
        raise ValueError("Checkpoint and application databases must be separate")
    database.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        await saver.setup()
        yield saver
