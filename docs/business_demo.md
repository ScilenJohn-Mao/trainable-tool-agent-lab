# 手工退款工具演示

在项目目录运行：

```powershell
uv run --no-sync --cache-dir .uv-cache python scripts/demo_business.py
```

脚本创建新的 `artifacts/demo/refund-<唯一标识>/app.sqlite3`，复用演示库初始化载入四订单和历史退款；保留本次业务记录供查询。普通运行库不被改写。也可用 `--database` 指定新的项目外绝对路径或以当前目录为基准的相对路径；已有文件、应用库与 checkpoint 库均拒绝，不重置数据。

脚本依次通过真实 MCP 查询 ORD-1001、检索并回读退款政策，通过可信执行器发布完整提案。终端显示商品、整数分金额、政策正文和完整提案后，输入 `approved` 批准或 `rejected` 拒绝；缺少输入或其他文本均不会记录批准。业务时间固定为 `2026-09-17T12:00:00+08:00`。

需要非交互演示时，操作者明确给出决定：

```powershell
uv run --no-sync --cache-dir .uv-cache python scripts/demo_business.py --decision approved
uv run --no-sync --cache-dir .uv-cache python scripts/demo_business.py --decision rejected
```

`--decision` 是操作者对本次展示提案的显式选择，仍经 ApprovalService 记录，不能替代业务规则检查。默认不自动批准。此入口固定演示 ORD-1001 的手工工具流程，不运行模型或训练。

批准后，执行器先提交 pending 操作键，真实 MCP 子进程复核批准与业务规则，在同事务消费确认、更新 12900 分退款和成功账本。脚本再按原键查询、重复同一调用并重新查订单，核对返回的原账本、余额、确认消费后，将本次任务/尝试及状态事件一并记为 completed。拒绝只记录决定、核对订单未变并完成任务，不调用退款工具。

stdout 输出一个 UTF-8 JSON 结果；提案、交互提示和 MCP 日志在 stderr。结果包括 task/attempt、完整 proposal/approval、前后订单、实际操作键与账本、确认消费时间、原键核对和重复调用结果、带 call_id 的工具调用结果。任务事件持久化每次工具调用及结果，成功调用成对关联；数据库中另有初始化的 ORD-1003 历史记录。

检索命中的文档引用与提案中的业务规格引用分别保留。政策正文提供说明依据，实际金额与授权仍由业务服务集中复核。

批准和拒绝的正常分支均返回 0；初始化、无有效决定、工具错误或核对失败返回非零，不输出成功报告。MCP 传输故障不会自动重试或标记 completed，pending 原键和未消费确认保留；需要按原键核对，不能将超时当成未执行。这里的原键查询和成功重复调用不表示已经实现故障自动恢复。

演示订单属于 `demo-user`；应用配置中的开发身份应与其一致。初始化路径保护见[演示库说明](demo_database.md)，执行边界见[可信执行器](executor.md)。
