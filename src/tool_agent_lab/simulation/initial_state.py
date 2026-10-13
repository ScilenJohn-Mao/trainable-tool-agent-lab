"""Reconstruct scenario balances through the existing approval and business services."""

from pathlib import Path

from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.business.service import BusinessService
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.runtime.task_service import TaskService, change_status
from tool_agent_lab.schemas.actions import ApprovalRequest, ExecutionContext, RefundAction, WriteBinding
from tool_agent_lab.schemas.scenarios import ScenarioInitialState
from tool_agent_lab.schemas.tasks import AttemptIdentity, TaskCreate, TaskStatus
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect, initialize_database, transaction
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository


def seed_initial_state(database: Path, initial: ScenarioInitialState, rules: BusinessRules) -> None:
    if database.exists():
        raise FileExistsError(f"Scenario database already exists: {database}")
    initialize_database(database)
    with transaction(database) as connection:
        for order in initial.orders:
            BusinessRepository(connection).add_order(order.model_copy(update={"refunded_amount_minor": 0, "coupon_amount_minor": 0}))
    orders = {order.order_id: order for order in initial.orders}
    for record in initial.historical_refunds:
        order = orders[record.order_id]
        tasks = TaskService(database, business_time=record.approved_at)
        task = tasks.create(TaskCreate(user_message="Historical approved refund", order_id=order.order_id), owner_id=order.owner_id)
        attempt = tasks.start(task.task_id, owner_id=task.owner_id)
        identity = AttemptIdentity(**{field: getattr(attempt, field) for field in AttemptIdentity.model_fields})
        action = RefundAction(order_id=order.order_id, amount_minor=order.paid_amount_minor,
                              reason="Historical verified delivery damage", policy_refs=(rules.reference("request_refund"),))
        proposal = tasks.propose(action, identity)
        approval = ApprovalService(database, business_time=record.approved_at).record(task.task_id, ApprovalRequest(
            request_id=record.approval_request_id, proposal_id=proposal.proposal_id,
            proposal_version=proposal.proposal_version, decision="approved",
        ), owner_id=task.owner_id)
        context = ExecutionContext(**identity.model_dump(), business_time=record.committed_at, write_binding=WriteBinding(
            proposal_id=proposal.proposal_id, proposal_version=proposal.proposal_version,
            approval_request_id=approval.request.request_id, operation_key=record.operation_key,
        ))
        service = BusinessService(database, rules)
        service.prepare(action, context)
        service.execute(action, context)
        with transaction(database) as connection:
            change_status(TaskRepository(connection), EventRepository(connection), attempt, TaskStatus.COMPLETED, record.committed_at)
    with connect(database) as connection:
        business = BusinessRepository(connection)
        if any(business.get_order(order.order_id, order.owner_id) != order for order in initial.orders):
            raise ValueError("Scenario balances do not match the approved historical ledger")
