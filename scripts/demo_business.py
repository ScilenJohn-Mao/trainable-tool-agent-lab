"""Demonstrate an operator-confirmed refund through the real local MCP tools."""

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.runtime.task_service import TaskService, change_status
from tool_agent_lab.schemas.actions import ApprovalDecision, ApprovalRequest
from tool_agent_lab.schemas.tasks import AttemptIdentity, TaskCreate, TaskStatus
from tool_agent_lab.settings import PROJECT_ROOT, Settings, load_settings
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect, transaction
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository
from tool_agent_lab.tools.executor import ToolExecutor

if __package__:
    from .seed_demo import seed_demo
else:
    from seed_demo import seed_demo


async def demo_refund(
    database: str | Path | None = None, *, decision: ApprovalDecision | str | None = None,
    settings: Settings | None = None,
) -> dict:
    """Run one manual tool workflow in a fresh database and retain its business records."""
    selected = ApprovalDecision(decision) if decision is not None else None
    settings = settings or load_settings()
    destination = database if database is not None else PROJECT_ROOT / f"artifacts/demo/refund-{uuid4().hex}/app.sqlite3"
    seeded = seed_demo(destination, settings=settings)
    database = Path(seeded["database"])
    rules = BusinessRules.from_file(settings.business_data_dir / "spec.json")
    tasks = TaskService(database, business_time=rules.business_time,
                        model_version="manual", config_version=settings.config_version)
    task = tasks.create(TaskCreate(user_message="ORD-1001 的杯子到货破损，请按核实记录退款。", order_id="ORD-1001"),
                        owner_id=settings.dev_owner_id)
    attempt = tasks.start(task.task_id, owner_id=task.owner_id)
    identity = AttemptIdentity(**{field: getattr(attempt, field) for field in AttemptIdentity.model_fields})
    executor = ToolExecutor(database, rules, identity, data_dir=settings.business_data_dir)
    calls = []

    async def call(name: str, arguments: dict):
        call_id = f"demo-{uuid4().hex}"
        with transaction(database) as connection:
            EventRepository(connection).append(identity, "tool_call", rules.business_time,
                                               call_id=call_id, payload={"tool": name, "arguments": arguments})
        reply = await executor.call_tool(name, arguments, call_id=call_id)
        record = {"tool": name, "arguments": arguments, "result": reply.result.model_dump(mode="json"),
                  "mcp_is_error": reply.raw.isError}
        calls.append(record)
        with transaction(database) as connection:
            EventRepository(connection).append(identity, "tool_result", rules.business_time,
                                               call_id=call_id, payload={"tool": name, "result": record["result"]})
        if reply.result.status != "ok":
            raise ValueError(f"{name}: {reply.result.error.code}")
        return reply.result.data

    before = await call("get_order", {"order_id": task.order_id})
    search = await call("search_policy", {
        "query": "全额退款 实付金额 CNY 整数分", "category": before.category, "limit": 3,
    })
    hit = next((hit for hit in search.hits if hit.reference.policy_id.startswith("P-REFUND-")), None)
    if hit is None:
        raise ValueError("No refund policy found for the demonstration")
    policy = await call("read_policy", {
        "policy_id": hit.reference.policy_id, "version": hit.reference.version, "section": hit.reference.section,
    })
    arguments = {"order_id": before.order_id, "amount_minor": before.paid_amount_minor,
                 "reason": "Verified delivery damage",
                 "policy_refs": [rules.reference("request_refund").model_dump(mode="json")]}
    proposal = executor.propose("request_refund", arguments)
    print(json.dumps({"order": before.model_dump(mode="json"), "policy": policy.model_dump(mode="json"),
                      "proposal": proposal.model_dump(mode="json")}, ensure_ascii=False, indent=2),
          file=sys.stderr, flush=True)
    if selected is None:
        print("Enter approved or rejected for this proposal:", file=sys.stderr, flush=True)
        answer = sys.stdin.readline()
        if not answer:
            raise EOFError("No operator decision received; proposal remains waiting_approval")
        selected = ApprovalDecision(answer.strip())
    approval = ApprovalService(database, business_time=rules.business_time).record(task.task_id, ApprovalRequest(
        request_id=f"operator-{uuid4().hex}", proposal_id=proposal.proposal_id,
        proposal_version=proposal.proposal_version, decision=selected,
    ), owner_id=task.owner_id)

    operation = lookup = repeated = None
    if selected == ApprovalDecision.APPROVED:
        operation = await call("request_refund", arguments)
        lookup = (await call("get_operation", {"operation_key": operation.operation_key})).operation
        repeated = await call("request_refund", arguments)
        if lookup is None or lookup.model_dump(mode="json") != operation.model_dump(mode="json"):
            raise ValueError("Operation lookup does not match the committed refund")
        if repeated.model_dump(mode="json") != operation.model_dump(mode="json"):
            raise ValueError("Repeated refund did not return the original operation")
    after = await call("get_order", {"order_id": before.order_id})
    with connect(database) as connection:
        stored = BusinessRepository(connection).get_successful_operation(before.order_id, "request_refund", task.owner_id)
        consumed = TaskRepository(connection).approval_consumed_at(task.task_id, approval.request.request_id, task.owner_id)
    if selected == ApprovalDecision.APPROVED:
        if (operation.status != "succeeded" or operation.amount_minor != before.paid_amount_minor
                or stored is None or stored.model_dump(mode="json") != operation.model_dump(mode="json")
                or after.refunded_amount_minor != before.paid_amount_minor
                or after.coupon_amount_minor != before.coupon_amount_minor or consumed is None):
            raise ValueError("Committed refund, order balance and consumed confirmation disagree")
    elif stored is not None or after != before or consumed is not None:
        raise ValueError("Rejected proposal changed the business state")
    with transaction(database) as connection:
        change_status(TaskRepository(connection), EventRepository(connection), attempt,
                      TaskStatus.COMPLETED, rules.business_time)
    return {"status": "refunded" if operation is not None else "rejected", "database": str(database),
            "workflow": "manual_tools", "business_time": rules.business_time.isoformat(), "amount_unit": "fen",
            "task": tasks.get(task.task_id, owner_id=task.owner_id).model_dump(mode="json"),
            "proposal": proposal.model_dump(mode="json"), "approval": approval.model_dump(mode="json"),
            "order_before": before.model_dump(mode="json"), "order_after": after.model_dump(mode="json"),
            "operation": operation.model_dump(mode="json") if operation else None,
            "approval_consumed_at": consumed.isoformat() if consumed else None,
            "original_key_lookup_matches": lookup is not None,
            "same_key_repeat_matches": repeated is not None, "calls": calls}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="New isolated database; relative paths use the current directory")
    parser.add_argument("--decision", choices=[decision.value for decision in ApprovalDecision],
                        help="Explicit operator decision; otherwise read it after displaying the proposal")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        report = asyncio.run(demo_refund(args.database, decision=args.decision))
    except (OSError, ValueError, RuntimeError, EOFError, sqlite3.Error) as error:
        print(f"Refund demonstration failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
