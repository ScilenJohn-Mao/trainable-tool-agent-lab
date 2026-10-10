"""Atomic execution leases for the local worker, using a real UTC clock."""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from tool_agent_lab.runtime.task_service import change_status
from tool_agent_lab.schemas.tasks import Attempt, TaskStatus
from tool_agent_lab.storage.database import connect, transaction
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Lease timestamps must include a timezone")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ExecutionLease:
    attempt: Attempt
    worker_id: str
    execution_epoch: int
    lease_expires_at: datetime
    heartbeat_at: datetime


class LeaseLost(RuntimeError):
    pass


class LeaseService:
    def __init__(self, database: str | Path, *, owner_id: str, model_version: str,
                 config_version: str, business_time: datetime, worker_id: str | None = None,
                 lease_seconds: float = 30.0) -> None:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        if worker_id is not None and not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        self.database = Path(database)
        self.owner_id = owner_id
        self.model_version = model_version
        self.config_version = config_version
        self.business_time = business_time
        self.worker_id = worker_id or f"worker-{uuid4().hex}"
        self.lease_seconds = lease_seconds

    def claim(self, *, task_id: str | None = None, now: datetime | None = None) -> ExecutionLease | None:
        """Claim queued work, or an explicitly selected running human reply; never reclaim a crash."""
        timestamp = _utc(now or utc_now())
        status = "running" if task_id is not None else "queued"
        with transaction(self.database) as connection:
            # One active execution per application database, including direct scheduler callers.
            if connection.execute(
                """SELECT 1 FROM attempts WHERE lease_worker_id IS NOT NULL
                AND julianday(lease_expires_at) > julianday(?) LIMIT 1""",
                (timestamp.isoformat(),),
            ).fetchone():
                return None
            row = connection.execute(
                """SELECT a.attempt_id FROM tasks t JOIN attempts a
                ON a.attempt_id=t.current_attempt_id AND a.task_id=t.task_id AND a.owner_id=t.owner_id
                WHERE t.owner_id=? AND t.status=? AND a.status=?
                AND a.model_version=? AND a.config_version=? AND a.lease_worker_id IS NULL
                AND (? IS NULL OR t.task_id=?) ORDER BY julianday(t.created_at), t.task_id LIMIT 1""",
                (self.owner_id, status, status, self.model_version, self.config_version, task_id, task_id),
            ).fetchone()
            if row is None:
                return None
            expires = timestamp + timedelta(seconds=self.lease_seconds)
            stored = connection.execute(
                """UPDATE attempts SET execution_epoch=execution_epoch+1, lease_worker_id=?,
                lease_expires_at=?, heartbeat_at=? WHERE attempt_id=? RETURNING *""",
                (self.worker_id, expires.isoformat(), timestamp.isoformat(), row["attempt_id"]),
            ).fetchone()
            tasks = TaskRepository(connection)
            attempt = tasks.get_attempt(stored["task_id"], stored["attempt_id"], self.owner_id)
            if status == "queued":
                change_status(tasks, EventRepository(connection), attempt, TaskStatus.RUNNING, self.business_time)
                attempt = tasks.get_attempt(attempt.task_id, attempt.attempt_id, attempt.owner_id)
            return ExecutionLease(attempt, self.worker_id, stored["execution_epoch"], expires, timestamp)

    def heartbeat(self, lease: ExecutionLease, *, now: datetime | None = None) -> ExecutionLease:
        """Extend only the same live epoch; an expired lease cannot be revived."""
        timestamp = _utc(now or utc_now())
        expires = timestamp + timedelta(seconds=self.lease_seconds)
        with transaction(self.database) as connection:
            changed = connection.execute(
                """UPDATE attempts SET lease_expires_at=?, heartbeat_at=?
                WHERE attempt_id=? AND task_id=? AND owner_id=? AND execution_epoch=?
                AND lease_worker_id=? AND julianday(lease_expires_at)>julianday(?)
                AND julianday(heartbeat_at)<=julianday(?) AND EXISTS (
                    SELECT 1 FROM tasks t WHERE t.task_id=attempts.task_id
                    AND t.current_attempt_id=attempts.attempt_id)
                """,
                (expires.isoformat(), timestamp.isoformat(), lease.attempt.attempt_id,
                 lease.attempt.task_id, lease.attempt.owner_id, lease.execution_epoch, lease.worker_id,
                 timestamp.isoformat(), timestamp.isoformat()),
            ).rowcount
            if changed != 1:
                raise LeaseLost("Execution lease is expired or no longer owned")
        return ExecutionLease(lease.attempt, lease.worker_id, lease.execution_epoch, expires, timestamp)

    def release(self, lease: ExecutionLease) -> bool:
        """Clear the matching lease while retaining the epoch and task's business status."""
        with transaction(self.database) as connection:
            return connection.execute(
                """UPDATE attempts SET lease_worker_id=NULL, lease_expires_at=NULL, heartbeat_at=NULL
                WHERE attempt_id=? AND task_id=? AND owner_id=? AND execution_epoch=? AND lease_worker_id=?""",
                (lease.attempt.attempt_id, lease.attempt.task_id, lease.attempt.owner_id,
                 lease.execution_epoch, lease.worker_id),
            ).rowcount == 1

    def expired(self, *, now: datetime | None = None) -> list[ExecutionLease]:
        """Read current compatible expired leases for subsequent reconciliation."""
        timestamp = _utc(now or utc_now())
        with connect(self.database) as connection:
            rows = connection.execute(
                """SELECT a.* FROM attempts a JOIN tasks t
                ON t.task_id=a.task_id AND t.current_attempt_id=a.attempt_id AND t.owner_id=a.owner_id
                WHERE a.owner_id=? AND a.model_version=? AND a.config_version=?
                AND a.lease_worker_id IS NOT NULL AND julianday(a.lease_expires_at)<=julianday(?)
                ORDER BY julianday(a.lease_expires_at), a.attempt_id""",
                (self.owner_id, self.model_version, self.config_version, timestamp.isoformat()),
            ).fetchall()
            tasks = TaskRepository(connection)
            return [ExecutionLease(
                tasks.get_attempt(row["task_id"], row["attempt_id"], row["owner_id"]),
                row["lease_worker_id"], row["execution_epoch"],
                datetime.fromisoformat(row["lease_expires_at"]), datetime.fromisoformat(row["heartbeat_at"]),
            ) for row in rows]
