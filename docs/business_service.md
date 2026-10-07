# 受保护业务执行接口

`BusinessService(database, rules).execute(action, context, clarification_attempted=False)`
执行模拟退款、延迟补偿或转人工，返回持久化的 `Operation`。数据库须已初始化。
`rules` 是从业务规格加载的 `BusinessRules`。

`action` 只包含业务参数；`ExecutionContext` 由可信运行时提供，绑定任务、归属、
尝试、线程、模型/配置版本、业务时间和 `WriteBinding`。不能将模型输出直接解析为
上下文并用作授权。`clarification_attempted` 也是运行时事实，仅在实际完成澄清后
设置；它不是工具的模型参数。订单无法确定且没有澄清事实时，转人工也会被拒绝。

调用前必须已保存任务、当前尝试、提案及完整批准快照。可信运行时为
`context.write_binding` 分配操作键，再调用 `service.prepare(action, context)`。
准备接口复用执行的授权与业务规则检查，在独立事务中保存 pending 记录；返回前
已经提交。此时不消费确认、不改变订单、不创建转人工结果。随后才能调用 execute。
服务不创建确认、不自动批准、不生成新操作键，也不隐式初始化库。
没有准备操作记录就执行时返回 `BusinessError("operation_not_prepared")`。

```python
# action and context are supplied by the trusted application runtime.
pending = service.prepare(action, context)
if pending.status == "pending":
    result = service.execute(action, context)
else:
    result = pending
stored = service.get_operation(pending.operation_key, context)
```

同键、同绑定重复准备会复用原记录及创建时间，不重置状态。原记录已成功或明确
失败时，prepare 返回其终态，不能把 failed 当成新的 pending 执行。相同批准换键
返回 `approval_already_bound`，必须沿用已保存的原键；同键换参数或批准返回
`operation_binding_mismatch`。其他归属占用同键时返回 `operation_key_conflict`，
不暴露其记录。新批准、新键也不能绕过已提交退款/补偿或转人工的业务去重。

`service.get_operation(operation_key, context)` 核对任务归属及完整尝试身份后，
返回该归属下的账本记录；未知或其他归属的键均返回 None。查询不需要 write_binding
或当前执行状态，也不要求政策仍有效，取消后的成功结果仍可读。它不消费确认或写库。
执行响应不确定时应按原键查询；pending 表示尚无已提交终态，不能自行改键或将超时
视为未执行。查询本身不会重试工具或恢复 worker。

执行会重新读取并核对：

- 任务归属、尝试的完整身份及当前执行状态；新写入仅允许 running/waiting_approval。
- 当前尝试最近追加的提案（每个提案 ID 只考虑最高版本）、实际参数与批准快照。
- 提案有效期、批准和准备时间，以及批准尚未消费。
- 操作键所绑定的任务、尝试、订单、动作、金额、币种和批准请求。
- 订单归属、最新业务状态、政策有效期、引用、金额与动作资格。

prepare 与 execute 分别打开事务，执行时会再次核对授权和最新业务状态。
确认消费、订单金额变更和操作账本完成使用同一个 SQLite BEGIN IMMEDIATE 事务。
转人工不改变金额，账本保存原因和摘要。退款与补偿分别提交，已完成退款不因后续
补偿失败而撤回。业务拒绝抛出具有 `.code` 的 `BusinessError`，保留原 pending
记录和未消费确认；存储异常原样传播并回滚整个执行事务。

同键成功结果在核对原提案、批准和操作绑定后直接返回，不重复写入；原结果在
任务取消或政策过期后仍可读。新的键和批准不能产生第二笔同订单退款/补偿。
同工单、订单（可为空）和原因的已成功转人工也不能重复创建。取消不撤销已提交动作。

这些是 Python 业务接口。集成测试通过存储接口准备合成确认记录，并在隔离数据库
核对实际写入，不代表完整用户确认入口、MCP 工具调用或响应丢失恢复流程。

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/integration/test_business_transactions.py
```
