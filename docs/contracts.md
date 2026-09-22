# 共享契约

`schemas/{tasks,actions,events}.py` 只依赖标准库和 Pydantic，提供结构校验与序列化，不执行数据库或业务操作。

| 契约 | 用途与约束 |
|---|---|
| `TaskCreate` / `Task` | 用户只提交消息和可选订单号；记录包含任务 ID、归属、当前尝试 ID、状态和创建时间。状态为 queued/running/waiting_input/waiting_approval/completed/failed/cancelled；schema 不执行状态迁移 |
| `AttemptIdentity` / `Attempt` | 绑定 task、owner、attempt、图 thread、模型和配置版本。手工预览使用 `manual` 标识 |
| `ActionParameters` | 按 `action` 区分退款、补偿和转人工。退款/补偿使用严格正整数 CNY 分，拒绝浮点、字符串和布尔金额；转人工没有金额字段。只有 `order_unresolved` 转人工省略订单号 |
| `PolicyReference` | 保留政策 ID、版本、引用位置和带时区的生效区间 `[effective_from, effective_to)`；只检查区间结构，适用性由业务规则核查 |
| `ActionProposal` | 保存 task/attempt、提案 ID、正整数版本、完整业务参数及创建时间；可选过期时间必须晚于创建时间。参数包含金额、理由、政策引用等，版本覆盖整个快照 |
| `ApprovalRequest` / `Approval` | 请求携带 request ID、提案 ID/版本和批准或拒绝。记录增加服务端解析的 owner、回复时间与完整提案快照，校验请求和快照的 ID/版本一致 |
| `WriteBinding` / `ExecutionContext` | 运行时身份、固定业务时间与可选写绑定。写绑定同时要求提案 ID/版本、确认请求 ID 和调用前已持久化的 operation_key；读取可以没有写绑定 |
| `Event` | 记录完整尝试身份、正整数 seq、事件类型、时间、可选 call_id 和 JSON payload；工具调用/结果事件必须提供 call_id。payload 按紧凑 JSON 的 UTF-8 编码限制为 16 KiB，大输出应另存并传引用 |

所有时间必须带时区，模型拒绝未知字段；身份与版本不隐式生成。业务参数和用户请求不接受 `owner_id`、确认标志或执行上下文等越界字段。确认快照中的动作与引用使用冻结模型和元组，避免无意原地修改。读取 JSON 使用 `model_validate_json()`，导出用 `model_dump_json()`，生成 JSON Schema 用 `model_json_schema()`。

创建 `Approval` 或 `ExecutionContext` 对象只验证结构，不代表授权已经成立。使用这些记录时，调用方负责校验可信用户、提案归属、有效版本、过期和单次消费，并核对保存的完整快照与调用参数。`expires_at=null` 表示没有设置时间过期，不免除版本和业务政策检查。模型提供业务参数，运行时附加可信上下文。

Schema 不保证跨记录的身份一致性、事务、单次消费、工具结果配对或事件序号递增。历史操作 JSON 是种子数据格式，不是业务执行器的免确认入口。

从项目目录预览一个 ORD-1001 的 12900 分退款契约快照：

```powershell
uv run --no-sync --cache-dir .uv-cache python scripts/check_contracts.py
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_schemas.py
```

检查命令从应用配置读取业务规格，完成 8 种记录的 JSON 往返，输出 UTF-8 JSON。样例确认由脚本构造，只用于展示结构；输出明确标注 `synthetic_contract_snapshots`、`approval_service: not_run` 和 `business_execution: not_run`，不创建数据库或修改订单。引用指向业务规格 `spec.json#/rules/refund`，不代表政策文档检索结果。
