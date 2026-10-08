# HTTP 任务与人工确认

从项目根目录启动本地服务：

```powershell
uv sync --locked --cache-dir .uv-cache
uv run --no-sync --cache-dir .uv-cache python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

OpenAPI JSON 位于 `/openapi.json`，交互文档位于 `/docs`。服务读取同一应用配置，
启动时初始化空应用库；已有数据库保留记录，不自动载入订单。模拟业务时间取自
`business_data_dir/spec.json`，任务尝试使用配置中的 mode/config_version。
`TTAL_RUNTIME_DIR` 可在当前进程环境中指定隔离运行目录。

服务端以 `dev_owner_id` 作为本地开发身份。HTTP body、query、header 中的 owner
不能切换身份；请求体中的额外字段返回 422。本接口用于本机单用户开发，监听地址
默认为回环地址。`create_app(settings)` 支持使用显式配置创建独立应用实例。

| 方法 | 路径 | 输入与结果 |
|---|---|---|
| POST | `/tasks` | `TaskCreate`：user_message、可选 order_id；201 返回 queued `Task` |
| GET | `/tasks` | 可选 status、limit（1–100，默认 50）、offset（≥0）；返回当前身份的任务列表 |
| GET | `/tasks/{task_id}` | 返回当前身份的任务；未知或其他身份的任务均为 404 |
| GET | `/tasks/{task_id}/proposal` | 返回完整当前 `ActionProposal`，无提案时为 null；先核对任务归属 |
| POST | `/tasks/{task_id}/approval` | `ApprovalRequest`：request_id、proposal_id、proposal_version、decision；200 返回完整 `Approval` |

`decision` 为 `approved` 或 `rejected`。提案由运行时通过 TaskService/ToolExecutor
产生，HTTP 不提供自行发布提案或写业务结果的入口。确认服务重读持久化快照，绑定
完整参数和版本；参数改变需确认新版本，截止时刻也算过期。确认只保存决定并将任务
恢复 running，不执行退款或消费批准；业务执行仍由受保护执行器完成。

同一个 request_id 和完全相同的决定重试返回原回执，不增加事件、重置批准消费或
重复执行业务。修改决定/请求绑定、旧版本、已决定提案与非等待状态返回 409。
未知/其他归属的任务及订单返回 404，服务错误采用
`{"detail":{"code":"task_not_found"}}` 等结构；请求结构非法采用 FastAPI 的 422
validation detail。存储故障由服务器记录并返回 500。

创建无需订单的工单并查询：

```powershell
$base = "http://127.0.0.1:8000"
$body = '{"user_message":"I need help locating my order"}'
$task = Invoke-RestMethod -Method Post -Uri "$base/tasks" -ContentType "application/json" -Body $body
Invoke-RestMethod -Uri "$base/tasks/$($task.task_id)"
Invoke-RestMethod -Uri "$base/tasks?status=queued&limit=10"
```

当运行时已发布提案后，先读取展示给操作者，再提交操作者的实际决定：

```powershell
$proposal = Invoke-RestMethod -Uri "$base/tasks/$($task.task_id)/proposal"
$decision = @{
    request_id = [guid]::NewGuid().ToString()
    proposal_id = $proposal.proposal_id
    proposal_version = $proposal.proposal_version
    decision = "approved"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$base/tasks/$($task.task_id)/approval" -ContentType "application/json" -Body $decision
```

重试确认时保留 `$decision` 中原 request_id。本服务提供任务和确认接口；模型、worker、
补充输入、取消、SSE 与任务终局处理由应用执行流程另外接入。
