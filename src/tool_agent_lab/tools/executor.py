"""Publish proposals and execute approved tools with runtime-owned bindings."""

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from mcp import types
from pydantic import TypeAdapter, ValidationError

from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.business.service import BusinessError, BusinessService
from tool_agent_lab.runtime.task_service import TaskService
from tool_agent_lab.schemas.actions import ActionProposal, ExecutionContext, WriteBinding
from tool_agent_lab.schemas.common import NonEmptyStr
from tool_agent_lab.schemas.tasks import AttemptIdentity
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect
from tool_agent_lab.storage.task_repository import TaskRepository
from tool_agent_lab.tools.client import ToolReply, local_server_parameters, open_tool_session
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS, ToolError, ToolFailure


class ToolExecutor:
    def __init__(
        self, database: str | Path, rules: BusinessRules, identity: AttemptIdentity, *,
        data_dir: str | Path, business_time: datetime | None = None,
        clarification_attempted: bool = False,
    ) -> None:
        self.database = Path(database).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.context = ExecutionContext(
            **identity.model_dump(), business_time=business_time or rules.business_time,
        )
        self.clarification_attempted = clarification_attempted
        self.business = BusinessService(self.database, rules)

    def propose(self, name: str, arguments: dict, *, expires_at: datetime | None = None) -> ActionProposal:
        contract = TOOL_CONTRACTS[name]
        if contract.read_only:
            raise ValueError("Only write tools have action proposals")
        action = contract.parse_arguments(arguments)
        identity = AttemptIdentity(**{
            field: getattr(self.context, field) for field in AttemptIdentity.model_fields
        })
        return TaskService(self.database, business_time=self.context.business_time).propose(
            action, identity, expires_at=expires_at,
        )

    def _prepare(self, action) -> ExecutionContext:
        context = self.context
        with connect(self.database) as connection:
            tasks = TaskRepository(connection)
            proposal = tasks.get_current_proposal(context.task_id, context.attempt_id, context.owner_id)
            if proposal is None:
                raise BusinessError("proposal_required")
            approval = tasks.get_proposal_approval(
                context.task_id, proposal.proposal_id, proposal.proposal_version, context.owner_id,
            )
            if approval is None:
                raise BusinessError("confirmation_required")
            existing = BusinessRepository(connection).get_operation_for_approval(
                approval.request.request_id, context.owner_id,
            )
        bound = context.model_copy(update={"write_binding": WriteBinding(
            proposal_id=proposal.proposal_id, proposal_version=proposal.proposal_version,
            approval_request_id=approval.request.request_id,
            operation_key=existing.operation_key if existing else f"operation-{uuid4().hex}",
        )})
        self.business.prepare(action, bound, clarification_attempted=self.clarification_attempted)
        return bound

    async def call_tool(self, name: str, arguments: dict, *, call_id: str | None = None) -> ToolReply:
        call_id = TypeAdapter(NonEmptyStr).validate_python(call_id if call_id is not None else str(uuid4()))
        contract = TOOL_CONTRACTS.get(name)
        try:
            if contract is None:
                raise BusinessError("unknown_tool")
            action = contract.parse_arguments(arguments)
            context = self.context if contract.read_only else self._prepare(action)
        except (BusinessError, ValidationError) as error:
            code = error.code if isinstance(error, BusinessError) else "invalid_arguments"
            result = ToolFailure(call_id=call_id, status="error", error=ToolError(
                code=code, message=code, outcome="not_committed",
            ))
            return ToolReply(raw=types.CallToolResult(
                content=[types.TextContent(type="text", text=result.model_dump_json())],
                structuredContent=result.model_dump(mode="json"), isError=True,
            ), result=result)
        parameters = local_server_parameters(
            database=self.database, data_dir=self.data_dir, owner_id=context.owner_id,
            execution_context=context, clarification_attempted=self.clarification_attempted,
        )
        async with open_tool_session(parameters) as client:
            return await client.call_tool(name, arguments, call_id=call_id)
