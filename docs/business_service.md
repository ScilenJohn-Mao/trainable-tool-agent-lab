# 受保护业务执行接口

`BusinessService(database, rules).execute(action, context, clarification_attempted=False)`
执行模拟退款、延迟补偿或转人工，返回持久化的 `Operation`。数据库须已初始化。
`rules` 是从业务规格加载的 `BusinessRules`。

`action` 只包含业务参数；`ExecutionContext` 由可信运行时提供，绑定任务、归属、
尝试、线程、模型/配置版本、业务时间和 `WriteBinding`。不能将模型输出直接解析为
上下文并用作授权。`clarification_attempted` 也是运行时事实，仅在实际完成澄清后
设置；它不是工具的模型参数。订单无法确定且没有澄清事实时，转人工也会被拒绝。

调用前必须已通过存储接口保存任务、当前尝试、提案、完整批准快照及 pending
操作记录。执行服务不创建确认、不自动批准、不生成新操作键，也不隐式初始化库。
没有准备操作记录时返回 `BusinessError("operation_not_prepared")`。

执行会重新读取并核对：

- 任务归属、尝试的完整身份及当前执行状态；新写入仅允许 running/waiting_approval。
- 当前尝试最近追加的提案（每个提案 ID 只考虑最高版本）、实际参数与批准快照。
- 提案有效期、批准和准备时间，以及批准尚未消费。
- 操作键所绑定的任务、尝试、订单、动作、金额、币种和批准请求。
- 订单归属、最新业务状态、政策有效期、引用、金额与动作资格。

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
