# 任务、提案与人工确认接口

数据库先由 `initialize_database` 初始化，订单由业务存储接口载入。
`TaskService(database, business_time=..., model_version="manual", config_version="app-v1")`
与 `ApprovalService(database, business_time=...)` 使用可信运行时提供的带时区业务时间，
模拟业务可直接使用 `BusinessRules.business_time`，不依赖机器当前日期。

任务服务提供：

- `create(TaskCreate(...), owner_id=...)`：创建 queued 任务及当前尝试，生成任务、尝试、
  线程 ID。owner 来自应用身份，不能从用户请求读取；指定的订单须属于该 owner。
- `get(task_id, owner_id=...)`、`list(owner_id=..., status=None, limit=50, offset=0)`：
  按归属查询，其他归属的任务返回 None 或空列表。
- `start(task_id, owner_id=...)`：queued → running，同时更新任务和尝试。
  已 running 的重复调用返回原尝试，不重复写事件；此接口不领取 worker 租约。
- `propose(action, identity, expires_at=None)`：核对可信尝试身份、状态及订单归属，
  保存完整提案并进入 waiting_approval。每次提出提案都追加版本，旧快照保留。
- `current_proposal(task_id, owner_id=...)`：读取当前尝试最新的完整提案，用于向用户展示。

`ApprovalService.record(task_id, ApprovalRequest(...), owner_id=...)` 接受用户的
request_id、proposal_id、proposal_version 和 approved/rejected，不能接收修改后的
业务参数、owner 或执行上下文。归属由应用的用户身份解析；模型工具不得调用此入口
给自己授权。服务重读当前提案，检查任务/尝试正等待确认、ID/版本及有效期，保存完整
快照。提案过期包含截止时刻，参数修改后必须确认新版本。

批准和拒绝都将任务/尝试恢复为 running，交由调用方继续处理对应分支；它们不会执行
退款、标记任务完成或消费批准。拒绝记录不能覆盖成批准；只有新版本、新请求才能
记录新的决定。业务服务会再次检查批准与规则，人工批准也不能允许错误金额。

同任务同 owner 重复提交完全相同的 request 返回原 Approval，不重复记事件或改变
消费状态，已经取消/更新提案之后也只返回原回执，不重新授权。同一 request_id
改变决定或绑定其他任务返回 `confirmation_request_conflict`；同提案换 request_id
返回 `proposal_already_decided`。调用方应保留原 request_id 重试。

提案、状态和事件同事务提交；决定、状态和事件也同事务提交，任一步失败则回滚。
拒绝原因以 `TaskError.code` 返回，存储异常原样传播。状态和确认事件只记录身份及
提案/请求引用，完整参数从持久化快照读取。

以下例子在临时业务库通过实际确认入口模拟本地操作者批准，不调用模型或工具传输。

```python
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.business.service import BusinessService
from tool_agent_lab.runtime.approvals import ApprovalService
from tool_agent_lab.runtime.task_service import TaskService
from tool_agent_lab.schemas.actions import ApprovalRequest, ExecutionContext, RefundAction, WriteBinding
from tool_agent_lab.schemas.business import Order
from tool_agent_lab.schemas.tasks import AttemptIdentity, TaskCreate
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import initialize_database, transaction

data = PROJECT_ROOT / "data/business/v1"
rules = BusinessRules.from_file(data / "spec.json")
with TemporaryDirectory() as directory:
    db = Path(directory) / "demo.sqlite3"
    initialize_database(db)
    with transaction(db) as connection:
        for order in json.loads((data / "orders.json").read_text(encoding="utf-8")):
            BusinessRepository(connection).add_order(Order.model_validate(order))
    tasks = TaskService(db, business_time=rules.business_time)
    task = tasks.create(TaskCreate(user_message="申请损坏订单退款", order_id="ORD-1001"), owner_id="demo-user")
    attempt = tasks.start(task.task_id, owner_id=task.owner_id)
    identity = AttemptIdentity(**{name: getattr(attempt, name) for name in AttemptIdentity.model_fields})
    action = RefundAction(order_id="ORD-1001", amount_minor=12900, reason="损坏已核实",
                          policy_refs=(rules.reference("request_refund"),))
    proposal = tasks.propose(action, identity)
    response = ApprovalRequest(request_id="local-user-response", proposal_id=proposal.proposal_id,
                               proposal_version=proposal.proposal_version, decision="approved")
    approval = ApprovalService(db, business_time=rules.business_time).record(
        task.task_id, response, owner_id=task.owner_id,
    )
    context = ExecutionContext(**identity.model_dump(), business_time=rules.business_time,
                               write_binding=WriteBinding(
                                   proposal_id=approval.proposal.proposal_id,
                                   proposal_version=approval.proposal.proposal_version,
                                   approval_request_id=approval.request.request_id,
                                   operation_key="local-refund-operation",
                               ))
    business = BusinessService(db, rules)
    pending = business.prepare(action, context)
    result = business.execute(action, context)
    assert business.get_operation(pending.operation_key, context) == result
    print(json.dumps({"status": result.status, "amount_minor": result.amount_minor}))
```

输出 `{"status": "succeeded", "amount_minor": 12900}`。临时库退出后删除。
任务保持 running，终局由应用流程单独处理；这里没有 API、MCP、Agent 或 worker 恢复。

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/integration/test_approvals.py
```
