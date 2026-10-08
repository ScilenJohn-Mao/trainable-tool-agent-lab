"""Build a fresh isolated demo database from the versioned business fixtures."""

import argparse
import json
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.business.service import BusinessService
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.runtime.task_service import TaskService, change_status
from tool_agent_lab.schemas.actions import ApprovalRequest, ExecutionContext, RefundAction, WriteBinding
from tool_agent_lab.schemas.business import Order
from tool_agent_lab.schemas.tasks import AttemptIdentity, TaskCreate, TaskStatus
from tool_agent_lab.settings import PROJECT_ROOT, Settings, load_settings
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import SCHEMA_VERSION, connect, initialize_database, transaction
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository

if __package__:
    from .check_business_data import check_business_data
else:
    from check_business_data import check_business_data


@contextmanager
def staging_database(parent: Path):
    # Normal mkdir inherits directory ACLs, unlike private tempfile directories.
    temporary = parent / f".seed-demo-{uuid4().hex}"
    temporary.mkdir()
    staged = temporary / "app.sqlite3"
    try:
        yield staged
    finally:
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(staged) + suffix).unlink(missing_ok=True)
        temporary.rmdir()


def seed_demo(database: str | Path | None = None, *, settings: Settings | None = None) -> dict:
    """Publish a new fixture database; never reset or overwrite an existing file."""
    settings = settings or load_settings()
    destination = Path(database if database is not None else PROJECT_ROOT / "artifacts/demo/app.sqlite3").resolve()
    protected = [settings.app_db_path, settings.checkpoint_db_path,
                 PROJECT_ROOT / "artifacts/runtime/app.sqlite3", PROJECT_ROOT / "artifacts/runtime/checkpoints.sqlite3"]
    if destination in {path.resolve() for path in protected}:
        raise ValueError("Demo database must differ from application and checkpoint databases")
    if destination.exists():
        raise FileExistsError(f"Demo database already exists; choose a new path: {destination}")

    data_dir = settings.business_data_dir
    checked = check_business_data(data_dir)
    orders = [Order.model_validate(row) for row in json.loads((data_dir / "orders.json").read_text(encoding="utf-8"))]
    history = json.loads((data_dir / "operations.json").read_text(encoding="utf-8"))
    rules = BusinessRules.from_file(data_dir / "spec.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    receipts = []
    with staging_database(destination.parent) as staged:
        initialize_database(staged)
        with transaction(staged) as connection:
            business = BusinessRepository(connection)
            for order in orders:
                business.add_order(order.model_copy(update={"refunded_amount_minor": 0, "coupon_amount_minor": 0}))

        for record in history:
            approved_at = datetime.fromisoformat(record["approval"]["approved_at"])
            committed_at = datetime.fromisoformat(record["committed_at"])
            tasks = TaskService(staged, business_time=approved_at)
            task = tasks.create(TaskCreate(user_message="Historical approved refund", order_id=record["order_id"]),
                                owner_id=record["owner_id"])
            attempt = tasks.start(task.task_id, owner_id=task.owner_id)
            identity = AttemptIdentity(**{field: getattr(attempt, field) for field in AttemptIdentity.model_fields})
            action = RefundAction(order_id=record["order_id"], amount_minor=record["amount_minor"],
                                  reason="Historical approved refund", policy_refs=(rules.reference("request_refund"),))
            proposal = tasks.propose(action, identity)
            approval = ApprovalService(staged, business_time=approved_at).record(task.task_id, ApprovalRequest(
                request_id=record["approval"]["request_id"], proposal_id=proposal.proposal_id,
                proposal_version=proposal.proposal_version, decision="approved",
            ), owner_id=task.owner_id)
            context = ExecutionContext(**identity.model_dump(), business_time=committed_at, write_binding=WriteBinding(
                proposal_id=proposal.proposal_id, proposal_version=proposal.proposal_version,
                approval_request_id=approval.request.request_id, operation_key=record["operation_key"],
            ))
            business_service = BusinessService(staged, rules)
            business_service.prepare(action, context)
            operation = business_service.execute(action, context)
            with transaction(staged) as connection:
                change_status(TaskRepository(connection), EventRepository(connection), attempt,
                              TaskStatus.COMPLETED, committed_at)
            receipts.append({"operation_key": operation.operation_key, "task_id": task.task_id,
                             "proposal_id": proposal.proposal_id,
                             "source_proposal_id": record["approval"]["proposal_id"],
                             "approval_request_id": approval.request.request_id})

        with connect(staged) as connection:
            business = BusinessRepository(connection)
            for order in orders:
                if business.get_order(order.order_id, order.owner_id) != order:
                    raise ValueError(f"Seed balances do not match fixture: {order.order_id}")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Seed foreign key check failed")
        # Exclusive creation also refuses a target created while the seed was being built.
        with destination.open("xb") as output:
            try:
                with staged.open("rb") as source:
                    shutil.copyfileobj(source, output)
            except BaseException:
                output.close()
                destination.unlink()
                raise

    return {"status": "demo_seeded", "database": str(destination), "schema_version": SCHEMA_VERSION,
            "data_version": checked["data_version"], "business_time": checked["business_time"],
            "amount_unit": "fen", "order_count": len(orders), "historical_operation_count": len(receipts),
            "historical_receipts": receipts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="Fresh demo database path; relative paths use the current directory")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        report = seed_demo(args.database)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Demo initialization failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
