"""Reconcile an interrupted worker's persisted graph with its owned business ledger."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from langgraph.types import StateSnapshot

from tool_agent_lab.runtime.task_service import TaskError, load_current_attempt
from tool_agent_lab.schemas.tasks import TaskStatus
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect
from tool_agent_lab.storage.task_repository import TaskRepository
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS

if TYPE_CHECKING:
    from tool_agent_lab.agent.graph import Agent


@dataclass(frozen=True)
class RecoveryPlan:
    task_id: str
    owner_id: str
    execution_epoch: int
    snapshot: StateSnapshot
    complete: bool


def recorded_reply(database, snapshot: StateSnapshot) -> str | None:
    """Resolve an existing human receipt, never invent a resume value."""
    state = snapshot.values
    identity = state["identity"]
    with connect(database) as connection:
        if snapshot.next == ("wait_approval",):
            proposal = state["proposal"]
            approval = TaskRepository(connection).get_proposal_approval(
                identity["task_id"], proposal["proposal_id"], proposal["proposal_version"], identity["owner_id"],
            )
            return approval.request.request_id if approval else None
        if snapshot.next == ("wait_input",):
            row = connection.execute(
                """SELECT json_extract(payload_json,'$.request_id') FROM events
                WHERE task_id=? AND attempt_id=? AND owner_id=? AND event_type='input_received'
                AND json_extract(payload_json,'$.input_request_id')=? ORDER BY seq DESC LIMIT 1""",
                (identity["task_id"], identity["attempt_id"], identity["owner_id"], state["waiting"]["request_id"]),
            ).fetchone()
            return row[0] if row else None
    return None


async def inspect_recovery(agent: Agent, task_id: str, *, owner_id: str) -> RecoveryPlan:
    """Inspect without mutating either database; claims are acquired only after validation."""
    initial, binding = agent.binding(task_id, owner_id)
    snapshot = await agent.graph.aget_state(binding)
    if snapshot.values and snapshot.values["identity"] != initial["identity"]:
        raise TaskError("checkpoint_identity_mismatch")
    with connect(agent.nodes.database) as connection:
        task, attempt = load_current_attempt(TaskRepository(connection), task_id, owner_id)
        if task.status != attempt.status:
            raise TaskError("recovery_status_mismatch")
        execution = connection.execute("SELECT execution_epoch, lease_worker_id FROM attempts WHERE attempt_id=?",
                                       (attempt.attempt_id,)).fetchone()
        epoch = execution["execution_epoch"]
        complete = bool(snapshot.values and not snapshot.next and task.status.value == snapshot.values["status"]
                        and task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
                        and execution["lease_worker_id"] is None)
        if complete:
            return RecoveryPlan(task_id, owner_id, epoch, snapshot, True)
        terminal_checkpoint = bool(snapshot.values and snapshot.values["result"] is not None
                                   and (snapshot.next == ("finish",) or not snapshot.next))
        if (epoch == 0 or task.status not in (TaskStatus.RUNNING, TaskStatus.WAITING_INPUT, TaskStatus.WAITING_APPROVAL)
                and not (terminal_checkpoint and task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED))):
            raise TaskError("task_not_recoverable")
        if not snapshot.values and connection.execute(
            "SELECT 1 FROM operations WHERE task_id=? AND attempt_id=? AND owner_id=? LIMIT 1",
            (task_id, attempt.attempt_id, owner_id),
        ).fetchone():
            raise TaskError("recovery_checkpoint_missing")
    return RecoveryPlan(task_id, owner_id, epoch, snapshot, False)


async def continue_recovery(agent: Agent, plan: RecoveryPlan) -> dict:
    """Continue on the original thread, restoring committed effects before graph invocation."""
    snapshot = plan.snapshot
    if not snapshot.values:
        return await agent.start(plan.task_id, owner_id=plan.owner_id)
    state = snapshot.values
    if snapshot.next in (("execute",), ("wait_approval",)) and state["active_call"] is not None:
        call = state["active_call"]
        contract = TOOL_CONTRACTS.get(call["function"]["name"])
        if contract is not None and contract.requires_approval and state["proposal"] is not None:
            with connect(agent.nodes.database) as connection:
                tasks = TaskRepository(connection)
                proposal = state["proposal"]
                approval = tasks.get_proposal_approval(
                    plan.task_id, proposal["proposal_id"], proposal["proposal_version"], plan.owner_id,
                )
                operation = BusinessRepository(connection).get_operation_for_approval(
                    approval.request.request_id, plan.owner_id,
                ) if approval else None
                if operation is not None and operation.status == "succeeded":
                    if (operation.task_id != plan.task_id or operation.attempt_id != state["identity"]["attempt_id"]
                            or approval.proposal.model_dump(mode="json") != proposal
                            or contract.parse_arguments(json.loads(call["function"]["arguments"])) != approval.proposal.parameters
                            or approval.request.decision != "approved"
                            or tasks.approval_consumed_at(plan.task_id, approval.request.request_id, plan.owner_id) is None):
                        raise TaskError("recovery_operation_mismatch")
                    result = contract.parse_result({"call_id": call["id"], "status": "ok",
                                                    "data": operation.model_dump(mode="json")}, call_id=call["id"])
                else:
                    result = None
            if result is not None:
                updates = agent.nodes.record_tool_result(state, result.model_dump(mode="json"))
                receipt = approval.model_dump(mode="json")
                updates["approval_receipts"] = state["approval_receipts"] + ([] if receipt in state["approval_receipts"] else [receipt])
                updates["status"] = "running"
                _, binding = agent.binding(plan.task_id, plan.owner_id)
                await agent.graph.aupdate_state(binding, updates, as_node="execute")
                return await agent.continue_task(plan.task_id, owner_id=plan.owner_id)
    if snapshot.interrupts:
        request_id = recorded_reply(agent.nodes.database, snapshot)
        if request_id:
            return await agent.resume(plan.task_id, request_id, owner_id=plan.owner_id)
    if not snapshot.next:
        with connect(agent.nodes.database) as connection:
            task, _ = load_current_attempt(TaskRepository(connection), plan.task_id, plan.owner_id)
        if task.status.value != state["status"]:
            agent.nodes.status(state, TaskStatus(state["status"]))
        return state
    state = await agent.continue_task(plan.task_id, owner_id=plan.owner_id)
    # A crash immediately before interrupt() may not yet have saved the waiting task.
    if state.get("__interrupt__"):
        waiting = await agent.snapshot(plan.task_id, owner_id=plan.owner_id)
        request_id = recorded_reply(agent.nodes.database, waiting)
        if request_id:
            return await agent.resume(plan.task_id, request_id, owner_id=plan.owner_id)
    return state
