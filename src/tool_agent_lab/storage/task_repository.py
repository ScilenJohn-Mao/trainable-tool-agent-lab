"""Persist owned tasks, attempts and versioned confirmation snapshots."""

import json
import sqlite3
from datetime import datetime

from tool_agent_lab.schemas.actions import ActionProposal, Approval, ApprovalRequest
from tool_agent_lab.schemas.tasks import Attempt, Task, TaskStatus
from tool_agent_lab.storage.database import require_transaction


class TaskRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add_task(self, task: Task) -> None:
        require_transaction(self.connection)
        self.connection.execute(
            """INSERT INTO tasks (task_id, owner_id, user_message, order_id,
                current_attempt_id, status, created_at)
            VALUES (:task_id, :owner_id, :user_message, :order_id,
                :current_attempt_id, :status, :created_at)""",
            task.model_dump(mode="json"),
        )

    def add_attempt(self, attempt: Attempt) -> None:
        require_transaction(self.connection)
        self.connection.execute(
            """INSERT INTO attempts (attempt_id, task_id, owner_id, thread_id,
                model_version, config_version, status, created_at)
            VALUES (:attempt_id, :task_id, :owner_id, :thread_id,
                :model_version, :config_version, :status, :created_at)""",
            attempt.model_dump(mode="json"),
        )

    def get_task(self, task_id: str, owner_id: str) -> Task | None:
        row = self.connection.execute(
            "SELECT * FROM tasks WHERE task_id = ? AND owner_id = ?", (task_id, owner_id)
        ).fetchone()
        return Task.model_validate(dict(row)) if row is not None else None

    def list_tasks(
        self, owner_id: str, *, status: TaskStatus | None = None, limit: int = 50, offset: int = 0
    ) -> list[Task]:
        if limit <= 0 or offset < 0:
            raise ValueError("limit must be positive and offset nonnegative")
        rows = self.connection.execute(
            """SELECT * FROM tasks WHERE owner_id = ? AND (? IS NULL OR status = ?)
                ORDER BY julianday(created_at) DESC, task_id LIMIT ? OFFSET ?""",
            (owner_id, status, status, limit, offset),
        )
        return [Task.model_validate(dict(row)) for row in rows]

    def get_attempt(self, task_id: str, attempt_id: str, owner_id: str) -> Attempt | None:
        row = self.connection.execute(
            "SELECT * FROM attempts WHERE task_id = ? AND attempt_id = ? AND owner_id = ?",
            (task_id, attempt_id, owner_id),
        ).fetchone()
        return Attempt.model_validate(dict(row)) if row is not None else None

    def set_status(self, task_id: str, owner_id: str, status: TaskStatus) -> bool:
        require_transaction(self.connection)
        return self.connection.execute(
            "UPDATE tasks SET status = ? WHERE task_id = ? AND owner_id = ?",
            (status, task_id, owner_id),
        ).rowcount == 1

    def set_current_attempt(self, task_id: str, owner_id: str, attempt_id: str) -> bool:
        require_transaction(self.connection)
        return self.connection.execute(
            "UPDATE tasks SET current_attempt_id = ? WHERE task_id = ? AND owner_id = ?",
            (attempt_id, task_id, owner_id),
        ).rowcount == 1

    def add_proposal(self, proposal: ActionProposal, owner_id: str) -> None:
        require_transaction(self.connection)
        if self.get_attempt(proposal.task_id, proposal.attempt_id, owner_id) is None:
            raise ValueError("Proposal attempt does not belong to owner")
        values = proposal.model_dump(mode="json")
        values["parameters_json"] = proposal.parameters.model_dump_json()
        self.connection.execute(
            """INSERT INTO proposals (proposal_id, proposal_version, task_id, attempt_id,
                parameters_json, created_at, expires_at)
            VALUES (:proposal_id, :proposal_version, :task_id, :attempt_id,
                :parameters_json, :created_at, :expires_at)""",
            values,
        )

    def get_proposal(
        self, task_id: str, proposal_id: str, owner_id: str, *, version: int | None = None
    ) -> ActionProposal | None:
        row = self.connection.execute(
            """SELECT p.* FROM proposals p JOIN tasks t ON t.task_id = p.task_id
                WHERE p.task_id = ? AND p.proposal_id = ? AND t.owner_id = ?
                    AND (? IS NULL OR p.proposal_version = ?)
                ORDER BY p.proposal_version DESC LIMIT 1""",
            (task_id, proposal_id, owner_id, version, version),
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        values["parameters"] = json.loads(values.pop("parameters_json"))
        return ActionProposal.model_validate(values)

    def get_current_proposal(
        self, task_id: str, attempt_id: str, owner_id: str
    ) -> ActionProposal | None:
        """Return the last appended proposal, considering only each ID's latest version."""
        row = self.connection.execute(
            """SELECT p.proposal_id, p.proposal_version FROM proposals p
                JOIN tasks t ON t.task_id = p.task_id
                WHERE p.task_id = ? AND p.attempt_id = ? AND t.owner_id = ?
                    AND p.proposal_version = (
                        SELECT MAX(v.proposal_version) FROM proposals v
                        WHERE v.proposal_id = p.proposal_id
                    )
                ORDER BY p.rowid DESC LIMIT 1""",
            (task_id, attempt_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        return self.get_proposal(task_id, row["proposal_id"], owner_id, version=row["proposal_version"])

    def add_approval(self, approval: Approval) -> None:
        require_transaction(self.connection)
        proposal = approval.proposal
        stored = self.get_proposal(
            proposal.task_id, proposal.proposal_id, approval.owner_id,
            version=proposal.proposal_version,
        )
        if stored != proposal:
            raise ValueError("Approval snapshot does not match the owned persisted proposal")
        self.connection.execute(
            """INSERT INTO approvals (request_id, proposal_id, proposal_version,
                owner_id, decision, proposal_json, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                approval.request.request_id, proposal.proposal_id, proposal.proposal_version,
                approval.owner_id, approval.request.decision, proposal.model_dump_json(),
                approval.decided_at.isoformat(),
            ),
        )

    def get_approval(self, task_id: str, request_id: str, owner_id: str) -> Approval | None:
        row = self.connection.execute(
            """SELECT a.* FROM approvals a
                JOIN proposals p USING (proposal_id, proposal_version)
                JOIN tasks t ON t.task_id = p.task_id
                WHERE p.task_id = ? AND a.request_id = ? AND a.owner_id = ? AND t.owner_id = ?""",
            (task_id, request_id, owner_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        return Approval(
            request=ApprovalRequest(
                request_id=row["request_id"], proposal_id=row["proposal_id"],
                proposal_version=row["proposal_version"], decision=row["decision"],
            ),
            proposal=ActionProposal.model_validate_json(row["proposal_json"]),
            owner_id=row["owner_id"], decided_at=row["decided_at"],
        )

    def consume_approval(
        self, task_id: str, request_id: str, owner_id: str, consumed_at: datetime
    ) -> bool:
        require_transaction(self.connection)
        if consumed_at.tzinfo is None or consumed_at.utcoffset() is None:
            raise ValueError("consumed_at must include a timezone")
        return self.connection.execute(
            """UPDATE approvals SET consumed_at = ?
                WHERE request_id = ? AND owner_id = ? AND decision = 'approved'
                    AND consumed_at IS NULL AND EXISTS (
                        SELECT 1 FROM proposals p JOIN tasks t ON t.task_id = p.task_id
                        WHERE p.proposal_id = approvals.proposal_id
                            AND p.proposal_version = approvals.proposal_version
                            AND p.task_id = ? AND t.owner_id = ?
                    )""",
            (consumed_at.isoformat(), request_id, owner_id, task_id, owner_id),
        ).rowcount == 1

    def approval_consumed_at(
        self, task_id: str, request_id: str, owner_id: str
    ) -> datetime | None:
        row = self.connection.execute(
            """SELECT a.consumed_at FROM approvals a
                JOIN proposals p USING (proposal_id, proposal_version)
                JOIN tasks t ON t.task_id = p.task_id
                WHERE p.task_id = ? AND a.request_id = ? AND a.owner_id = ? AND t.owner_id = ?""",
            (task_id, request_id, owner_id, owner_id),
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row is not None and row[0] is not None else None
