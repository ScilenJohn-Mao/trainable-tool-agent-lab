"""Verify persistence, transaction boundaries and business uniqueness constraints."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tool_agent_lab.storage import database

NOW = "2026-09-17T12:00:00+08:00"


def insert_order(connection: sqlite3.Connection) -> None:
    connection.execute(
        """INSERT INTO orders (
            order_id, owner_id, product_name, category, paid_amount_minor,
            promised_delivery_at, delivered_at, damage_verified
        ) VALUES ('order-1', 'owner-1', 'Cup', 'general_goods', 12900, ?, ?, 1)""",
        (NOW, NOW),
    )


def insert_task(connection: sqlite3.Connection, task_id: str = "task-1") -> None:
    connection.execute(
        "INSERT INTO tasks (task_id, owner_id, user_message, created_at) VALUES (?, 'owner-1', 'Refund', ?)",
        (task_id, NOW),
    )
    connection.execute(
        """INSERT INTO attempts (
            task_id, attempt_id, owner_id, thread_id, model_version, config_version, created_at
        ) VALUES (?, ?, 'owner-1', ?, 'manual', 'app-v1', ?)""",
        (task_id, f"attempt-{task_id}", f"thread-{task_id}", NOW),
    )


def insert_approval(connection: sqlite3.Connection, suffix: str = "1", version: int = 1) -> None:
    connection.execute(
        """INSERT INTO proposals (
            proposal_id, proposal_version, task_id, attempt_id, parameters_json, created_at
        ) VALUES (?, ?, 'task-1', 'attempt-task-1', ?, ?)""",
        (f"proposal-{suffix}", version, '{"action":"request_refund","amount_minor":12900}', NOW),
    )
    connection.execute(
        """INSERT INTO approvals (
            request_id, proposal_id, proposal_version, owner_id, decision, proposal_json, decided_at
        ) VALUES (?, ?, ?, 'owner-1', 'approved', ?, ?)""",
        (
            f"approval-{suffix}-{version}", f"proposal-{suffix}", version,
            '{"parameters":{"amount_minor":12900}}', NOW,
        ),
    )


def insert_operation(
    connection: sqlite3.Connection,
    key: str = "operation-1",
    approval: str = "approval-1-1",
    action: str = "request_refund",
    status: str = "succeeded",
    amount: int | float | None = 12900,
) -> None:
    connection.execute(
        """INSERT INTO operations (
            operation_key, task_id, attempt_id, owner_id, order_id, action, amount_minor,
            approval_request_id, status, created_at, committed_at
        ) VALUES (?, 'task-1', 'attempt-task-1', 'owner-1', 'order-1', ?, ?, ?, ?, ?, ?)""",
        (key, action, amount, approval, status, NOW, NOW if status == "succeeded" else None),
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime" / "app.sqlite3"
    assert database.initialize_database(path) == 2
    with database.transaction(path) as connection:
        insert_order(connection)
        insert_task(connection)
        insert_approval(connection)
    return path


def test_reinitialization_preserves_records_and_separates_checkpoint(db_path: Path) -> None:
    assert database.initialize_database(db_path) == 2
    with database.connect(db_path) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT paid_amount_minor FROM orders").fetchone()[0] == 12900
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not db_path.with_name("checkpoints.sqlite3").exists()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_failed_migration_rolls_back_ddl_and_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    migration = tmp_path / "broken.sql"
    migration.write_text("CREATE TABLE partial (id INTEGER);\nINVALID SQL;\n", encoding="utf-8")
    monkeypatch.setattr(database, "INITIAL_MIGRATION", migration)
    path = tmp_path / "app.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        database.initialize_database(path)
    with database.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_unsupported_version_is_preserved(db_path: Path) -> None:
    with database.connect(db_path) as connection:
        connection.execute("PRAGMA user_version = 3")
    with pytest.raises(ValueError, match="version: 3"):
        database.initialize_database(db_path)
    with database.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_business_change_and_ledger_commit_together(db_path: Path) -> None:
    with database.transaction(db_path) as connection:
        connection.execute("UPDATE orders SET refunded_amount_minor = 12900")
        insert_operation(connection)
    with database.connect(db_path) as connection:
        assert connection.execute("SELECT refunded_amount_minor FROM orders").fetchone()[0] == 12900
        assert connection.execute("SELECT amount_minor FROM operations").fetchone()[0] == 12900


def test_business_change_and_ledger_roll_back_together(db_path: Path) -> None:
    with pytest.raises(RuntimeError, match="stop before commit"):
        with database.transaction(db_path) as connection:
            connection.execute("UPDATE orders SET refunded_amount_minor = 12900")
            insert_operation(connection)
            raise RuntimeError("stop before commit")
    with database.connect(db_path) as connection:
        assert connection.execute("SELECT refunded_amount_minor FROM orders").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0


def test_duplicate_key_and_changed_key_cannot_duplicate_refund(db_path: Path) -> None:
    with database.transaction(db_path) as connection:
        insert_operation(connection)
        insert_approval(connection, "2")
    for key, status, constraint in [
        ("operation-1", "pending", "operations.operation_key"),
        ("operation-2", "succeeded", "operations.order_id, operations.action"),
    ]:
        with pytest.raises(sqlite3.IntegrityError, match=constraint):
            with database.transaction(db_path) as connection:
                connection.execute("UPDATE orders SET coupon_amount_minor = 500")
                insert_operation(connection, key, "approval-2-1", status=status)
    with database.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        assert connection.execute("SELECT coupon_amount_minor FROM orders").fetchone()[0] == 0


def test_refund_and_coupon_are_independent_but_coupon_cannot_repeat(db_path: Path) -> None:
    with database.transaction(db_path) as connection:
        insert_operation(connection)
        insert_approval(connection, "coupon")
        insert_operation(connection, "coupon-1", "approval-coupon-1", "issue_coupon", amount=500)
        insert_approval(connection, "duplicate")
    with pytest.raises(sqlite3.IntegrityError, match="operations.order_id, operations.action"):
        with database.transaction(db_path) as connection:
            insert_operation(connection, "coupon-2", "approval-duplicate-1", "issue_coupon", amount=500)


def test_pending_and_failed_operations_do_not_block_first_success(db_path: Path) -> None:
    with database.transaction(db_path) as connection:
        insert_operation(connection, status="pending")
        connection.execute("UPDATE operations SET status = 'failed' WHERE operation_key = 'operation-1'")
        insert_approval(connection, "retry")
        insert_operation(connection, "retry", "approval-retry-1")
    with database.connect(db_path) as connection:
        statuses = connection.execute("SELECT status FROM operations ORDER BY operation_key").fetchall()
        assert [row[0] for row in statuses] == ["failed", "succeeded"]


@pytest.mark.parametrize("amount", [None, -1, 0, 12.5])
def test_invalid_money_cannot_enter_ledger(db_path: Path, amount: int | float | None) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction(db_path) as connection:
            insert_operation(connection, amount=amount)


@pytest.mark.parametrize("value", [-1, 12901, 12.5])
def test_refund_balance_is_integer_and_within_paid_amount(db_path: Path, value: int | float) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction(db_path) as connection:
            connection.execute("UPDATE orders SET refunded_amount_minor = ?", (value,))


def test_confirmation_is_versioned_and_each_approval_binds_one_operation(db_path: Path) -> None:
    with database.transaction(db_path) as connection:
        insert_approval(connection, version=2)
        assert connection.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 2
        insert_operation(connection, status="pending")
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction(db_path) as connection:
            insert_operation(connection, "another-key", status="pending")
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction(db_path) as connection:
            connection.execute("UPDATE approvals SET proposal_version = 99 WHERE request_id = 'approval-1-1'")
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction(db_path) as connection:
            connection.execute(
                """INSERT INTO approvals SELECT 'duplicate', proposal_id, proposal_version,
                    owner_id, decision, proposal_json, decided_at, consumed_at
                    FROM approvals WHERE request_id = 'approval-1-2'"""
            )


def test_deferred_current_attempt_binding_and_failed_commit_rollback(db_path: Path) -> None:
    with database.transaction(db_path) as connection:
        connection.execute("UPDATE tasks SET current_attempt_id = 'attempt-task-1'")
        insert_task(connection, "task-2")
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction(db_path) as connection:
            connection.execute("UPDATE orders SET coupon_amount_minor = 500")
            connection.execute(
                "UPDATE tasks SET current_attempt_id = 'attempt-task-2' WHERE task_id = 'task-1'"
            )
    with database.connect(db_path) as connection:
        assert connection.execute("SELECT coupon_amount_minor FROM orders").fetchone()[0] == 0


def test_events_have_per_task_sequences_and_attempt_binding(db_path: Path) -> None:
    with database.transaction(db_path) as connection:
        insert_task(connection, "task-2")
        for task_id in ("task-1", "task-2"):
            connection.execute(
                """INSERT INTO events (task_id, seq, attempt_id, owner_id, thread_id,
                    model_version, config_version, event_type, occurred_at)
                VALUES (?, 1, ?, 'owner-1', ?, 'manual', 'app-v1', 'task_status_changed', ?)""",
                (task_id, f"attempt-{task_id}", f"thread-{task_id}", NOW),
            )
    for update in [
        "INSERT INTO events SELECT * FROM events WHERE task_id = 'task-1'",
        "UPDATE events SET attempt_id = 'attempt-task-2' WHERE task_id = 'task-1'",
        "UPDATE events SET payload_json = 'invalid' WHERE task_id = 'task-1'",
        "UPDATE events SET event_type = 'tool_call' WHERE task_id = 'task-1'",
    ]:
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction(db_path) as connection:
                connection.execute(update)


def test_cli_from_other_directory_reinitializes_unicode_path(tmp_path: Path) -> None:
    path = tmp_path / "订单" / "app.sqlite3"
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-m", "tool_agent_lab.storage.database", "--database", str(path)],
            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=True,
        )
        assert json.loads(result.stdout) == {"database": str(path), "schema_version": 2}
    assert not path.with_name("checkpoints.sqlite3").exists()


def create_previous_database(path: Path) -> dict[str, list[tuple]]:
    with database.connect(path) as connection:
        connection.executescript(database.INITIAL_MIGRATION.read_text(encoding="utf-8"))
        connection.execute("PRAGMA user_version = 1")
    with database.transaction(path) as connection:
        insert_order(connection)
        insert_task(connection)
        connection.execute("UPDATE tasks SET current_attempt_id = 'attempt-task-1', status = 'running'")
        connection.execute("UPDATE attempts SET status = 'running'")
        insert_approval(connection)
        insert_operation(connection, status="pending")
        connection.execute(
            """INSERT INTO events (task_id, seq, attempt_id, owner_id, thread_id,
                model_version, config_version, event_type, occurred_at)
            VALUES ('task-1', 1, 'attempt-task-1', 'owner-1', 'thread-task-1',
                'manual', 'app-v1', 'task_status_changed', ?)""", (NOW,),
        )
        return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in ("orders", "tasks", "attempts", "proposals", "approvals", "operations", "events")}


def test_previous_database_upgrade_preserves_running_task_and_pending_operation(tmp_path: Path) -> None:
    path = tmp_path / "previous.sqlite3"
    previous = create_previous_database(path)
    assert database.initialize_database(path) == 2
    assert database.initialize_database(path) == 2
    with database.connect(path) as connection:
        for table, rows in previous.items():
            current = [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            if table == "attempts":
                assert [row[:-4] for row in current] == rows
                assert current[0][-4:] == (0, None, None, None)
            else:
                assert current == rows
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("previous_version", [0, 1])
def test_failed_execution_migration_rolls_back_columns_data_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previous_version: int,
) -> None:
    path = tmp_path / "app.sqlite3"
    if previous_version:
        previous = create_previous_database(path)
    migration = tmp_path / "broken_execution.sql"
    migration.write_text(database.EXECUTION_MIGRATION.read_text(encoding="utf-8") + "INVALID SQL;\n", encoding="utf-8")
    monkeypatch.setattr(database, "EXECUTION_MIGRATION", migration)
    with pytest.raises(sqlite3.OperationalError):
        database.initialize_database(path)
    with database.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == previous_version
        if previous_version:
            assert "execution_epoch" not in {row[1] for row in connection.execute("PRAGMA table_info(attempts)")}
            assert connection.execute("SELECT name FROM sqlite_master WHERE name='idx_attempts_lease_expiry'").fetchone() is None
            for table, rows in previous.items():
                assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] == rows
        else:
            assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_execution_lease_persists_and_release_retains_epoch(db_path: Path) -> None:
    for update in (
        "execution_epoch = -1",
        "lease_worker_id = 'worker-1'",
        "execution_epoch = 1, lease_worker_id = '', lease_expires_at = 'later', heartbeat_at = 'now'",
        "lease_worker_id = 'worker-1', lease_expires_at = 'later', heartbeat_at = 'now'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction(db_path) as connection:
                connection.execute(f"UPDATE attempts SET {update}")
    with database.transaction(db_path) as connection:
        connection.execute(
            """UPDATE attempts SET execution_epoch=1, lease_worker_id='worker-1',
                lease_expires_at=?, heartbeat_at=?""", ("2026-09-17T12:01:00+08:00", NOW),
        )
    assert database.initialize_database(db_path) == 2
    with database.connect(db_path) as connection:
        row = connection.execute("SELECT execution_epoch, lease_worker_id, lease_expires_at, heartbeat_at FROM attempts").fetchone()
        assert tuple(row) == (1, "worker-1", "2026-09-17T12:01:00+08:00", NOW)
    with database.transaction(db_path) as connection:
        connection.execute("UPDATE attempts SET lease_worker_id=NULL, lease_expires_at=NULL, heartbeat_at=NULL")
    with database.connect(db_path) as connection:
        assert connection.execute("SELECT execution_epoch FROM attempts").fetchone()[0] == 1
