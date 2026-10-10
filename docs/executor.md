# Trusted tool execution

`ToolExecutor(database, rules, identity, data_dir=..., business_time=None,
clarification_attempted=False)` accepts application-owned attempt identity and time.
The model only supplies a tool name and its business arguments. Identity, approval,
operation keys and clarification facts are never model arguments.

`propose(name, arguments, expires_at=None)` uses TaskService to persist a versioned
write proposal and enter waiting_approval. It does not approve or execute it.
The application displays the proposal and passes the user's actual decision to
ApprovalService. Changed parameters require a new proposal and confirmation.

`await call_tool(name, arguments, call_id=None)` returns ToolReply with the original
MCP result and a matching typed envelope. For writes it resolves the current persisted
proposal and confirmation, reuses the operation associated with that confirmation or
generates a UUID key, then calls BusinessService.prepare. The pending key commits
before starting the MCP subprocess; money and confirmation remain unchanged there.

Invalid model arguments return `invalid_arguments` with the failing field and its
validation reason in `error.message`, without echoing input values. For example,
an empty order ID reports that `order_id` requires at least one character. It does
not substitute an order ID or relax the shared argument schema.

The child receives ExecutionContext in its startup environment (TTAL_MCP_CONTEXT),
separate from MCP arguments and metadata. TTAL_MCP_CLARIFIED carries the runtime's
clarification fact. The standard client does not inherit either reserved variable;
only explicit runtime parameters add them. A local process launcher is trusted,
just as it is trusted to choose a database and owner. This is a local stdio boundary,
not authentication for arbitrary remote clients.

The server calls BusinessService.execute, rechecking persisted authorization and
rules. Approval consumption, balance changes and ledger success commit together.
Read-only get_operation uses BusinessService to check complete attempt identity.
Other reads use the startup owner and the runtime policy clock. A successful retry
with the same current proposal/confirmation returns its original operation key.
The executor never approves an action or changes a task to completed.

Preparation refusals return not_committed and a matching call_id without starting a
child. MCP refusals preserve raw structured output. Transport failures propagate;
automatic recovery and uncertain-response reconciliation are not implemented here.
The pending ledger remains available by confirmation and original key; do not issue
a new proposal/key merely because a response was lost.

## Isolated Python example

The example creates a disposable business instance; order loading is initialization,
while proposal, approval and refund use the public protected services.

```python
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.runtime.task_service import TaskService
from tool_agent_lab.schemas.actions import ApprovalRequest
from tool_agent_lab.schemas.business import Order
from tool_agent_lab.schemas.tasks import AttemptIdentity, TaskCreate
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import initialize_database, transaction
from tool_agent_lab.tools.executor import ToolExecutor

async def main():
    data = PROJECT_ROOT / "data/business/v1"
    rules = BusinessRules.from_file(data / "spec.json")
    with TemporaryDirectory(prefix="ttal-business-example-") as temporary:
        database = Path(temporary) / "business.sqlite3"
        initialize_database(database)
        with transaction(database) as connection:
            for row in json.loads((data / "orders.json").read_text(encoding="utf-8")):
                BusinessRepository(connection).add_order(Order.model_validate(row))
        tasks = TaskService(database, business_time=rules.business_time)
        task = tasks.create(TaskCreate(user_message="Refund verified damage", order_id="ORD-1001"),
                            owner_id="demo-user")
        attempt = tasks.start(task.task_id, owner_id="demo-user")
        identity = AttemptIdentity(**{name: getattr(attempt, name) for name in AttemptIdentity.model_fields})
        executor = ToolExecutor(database, rules, identity, data_dir=data)
        arguments = {"order_id": "ORD-1001", "amount_minor": 12900, "reason": "verified damage",
                     "policy_refs": [rules.reference("request_refund").model_dump(mode="json")]}
        proposal = executor.propose("request_refund", arguments)
        # The local operator chooses approved for this example's displayed proposal.
        ApprovalService(database, business_time=rules.business_time).record(task.task_id, ApprovalRequest(
            request_id="operator-confirmation", proposal_id=proposal.proposal_id,
            proposal_version=proposal.proposal_version, decision="approved",
        ), owner_id="demo-user")
        reply = await executor.call_tool("request_refund", arguments, call_id="refund-example")
        assert reply.result.status == "ok"
        key = reply.result.data.operation_key
        lookup = await executor.call_tool("get_operation", {"operation_key": key})
        assert lookup.result.data.operation.status == "succeeded"
        assert lookup.result.data.operation.amount_minor == 12900
        print(reply.result.model_dump_json())

asyncio.run(main())
```
