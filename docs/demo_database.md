# 演示业务库

`scripts/seed_demo.py` 从配置的 `business_data_dir` 创建新的隔离演示库，使用现有 SQLite schema、repository、任务/确认服务及业务服务，不需要模型依赖。

```powershell
uv run --no-sync --cache-dir .uv-cache python scripts/seed_demo.py
uv run --no-sync --cache-dir .uv-cache python scripts/seed_demo.py --database artifacts/demo/refund-example/app.sqlite3
```

默认路径是源码项目下的 `artifacts/demo/app.sqlite3`，与应用默认运行库分开。`--database` 相对路径以当前工作目录为基准，也可指定项目外绝对路径。脚本拒绝当前配置及默认位置的应用库/checkpoint 库，拒绝任何已有目标文件；重复运行不覆盖订单、任务、确认或账本。每个业务样例使用一个新目录或文件名，脚本不提供重置/删除功能。

数据先在临时库中构建，核对余额和外键后才发布到新目标；初始化失败不会发布部分业务数据。输出 UTF-8 JSON，包含绝对库路径、schema/data 版本、固定业务时间、金额单位、订单/历史操作数量及历史回执。失败返回非零退出码。

库内包含四个订单，CNY 金额使用整数分：ORD-1001 尚未退款，退款金额 12900 分；ORD-1002 尚未退款或补偿，延迟补偿金额 500 分；ORD-1003 已有 9900 分历史退款；ORD-1004 是数字商品。

历史退款由 `operations.json` 中的已记录决定重建：先载入未退款订单，经 TaskService 创建任务/提案、ApprovalService 记录历史决定，再由 BusinessService.prepare/execute 提交原键 `refund-ORD-1003-original`，同事务消费确认并更新余额。批准时间保持 `2026-09-17T10:59:00+08:00`、提交时间保持 `2026-09-17T11:00:00+08:00`。历史任务与尝试记为 completed。任务/提案 ID 使用服务生成的新 ID，报告中的 `source_proposal_id` 对照数据来源；原操作键、确认请求 ID、金额和时间保持。历史数据重建不向新任务授予批准。

后续任务使用规格固定业务时间 `2026-09-17T12:00:00+08:00`，同样需要发布提案、记录人工决定，再经可信执行器调用 MCP。预置账本只代表已完成的历史业务状态。

让 HTTP 应用读取默认演示库时，先完成初始化，再在当前 PowerShell 会话中选择运行目录：

```powershell
$previousRuntime = $env:TTAL_RUNTIME_DIR
try {
    $env:TTAL_RUNTIME_DIR = 'artifacts/demo'
    uv run --no-sync --cache-dir .uv-cache python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
} finally {
    $env:TTAL_RUNTIME_DIR = $previousRuntime
}
```

演示订单属于 `demo-user`，HTTP 开发身份应与其一致。服务端配置与接口见 [HTTP 说明](http_api.md)。应用会保留演示库已有记录；需要另一个独立样例时重新初始化新路径。
