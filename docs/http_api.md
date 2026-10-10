# HTTP 任务与人工确认

从项目根目录启动本地服务：

```powershell
uv sync --locked --cache-dir .uv-cache
uv run --no-sync --cache-dir .uv-cache python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

OpenAPI JSON 位于 `/openapi.json`，交互文档位于 `/docs`。服务读取同一应用配置，
启动时初始化空应用库；已有数据库保留记录，不自动载入订单。模拟业务时间取自
`business_data_dir/spec.json`，任务尝试使用 `model_config_file` 中的模型 version 与
`agent_config_file` 中的 Agent version，API 启动只读配置、不加载权重或 GPU 库。
API 与独立 worker 使用相同配置和运行目录；默认选用 mock。真实本机模型可通过
`TTAL_MODEL_CONFIG_FILE=configs/models/qwen3b-local.yaml` 选择，worker 使用独立推理环境。
`TTAL_RUNTIME_DIR` 可在当前进程环境中指定隔离运行目录。

服务端以 `dev_owner_id` 作为本地开发身份。HTTP body、query、header 中的 owner
不能切换身份；请求体中的额外字段返回 422。本接口用于本机单用户开发，监听地址
默认为回环地址。`create_app(settings)` 支持使用显式配置创建独立应用实例。

| 方法 | 路径 | 输入与结果 |
|---|---|---|
| POST | `/tasks` | `TaskCreate`：user_message、可选 order_id；201 返回 queued `Task` |
| GET | `/tasks` | 可选 status、limit（1–100，默认 50）、offset（≥0）；返回当前身份的任务列表 |
| GET | `/tasks/{task_id}` | 返回任务字段、attempt、input_request 与 result；未知或其他身份的任务均为 404 |
| GET | `/tasks/{task_id}/proposal` | 返回完整当前 `ActionProposal`，无提案时为 null；先核对任务归属 |
| POST | `/tasks/{task_id}/approval` | `ApprovalRequest`：request_id、proposal_id、proposal_version、decision；200 返回完整 `Approval` |
| POST | `/tasks/{task_id}/input` | `InputRequest`：request_id、input_request_id、message；200 返回保存的输入回执 |
| POST | `/tasks/{task_id}/cancel` | 无请求体；取消 queued/waiting_input/waiting_approval，200 返回 cancelled 任务 |

`input_request` 在 waiting_input 状态返回 `{kind, request_id, question}`，其他状态为
null。`attempt` 包含 thread_id、模型/Agent 配置版本和持久化状态。`result` 从该尝试的
SQLite checkpoint 读取，包含实际账本操作、金额、事实和引用；未完成或无已保存结果时
为 null。应用状态和 checkpoint 分别提交，终态刚出现时结果可能短暂为 null，稍后查询
即可读取；执行异常未生成结构化结果时也为 null。查询不会加载模型或启动图。

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

重试确认时保留 `$decision` 中原 request_id。补充输入只接受当前等待点的标识，
message 最多 4000 字符。保存后恢复 running，由 worker 继续原线程；相同请求精确
重试只返回原回执，修改内容返回 `input_request_conflict`，旧等待点返回
`input_request_not_current`，非等待状态返回 `task_not_waiting_input`。

```powershell
$detail = Invoke-RestMethod -Uri "$base/tasks/$($task.task_id)"
$inputReply = @{
    request_id = [guid]::NewGuid().ToString()
    input_request_id = $detail.input_request.request_id
    message = "ORD-1001"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$base/tasks/$($task.task_id)/input" -ContentType "application/json" -Body $inputReply
```

取消排队或等待人工交互的任务：

```powershell
Invoke-RestMethod -Method Post -Uri "$base/tasks/$($task.task_id)/cancel"
```

取消同步写入任务、尝试及状态事件；重复取消直接返回原任务。running/completed/failed
返回 409 `task_not_cancellable`，不强行中断正在执行的模型/工具，也不撤销已提交账本。
取消后新的输入或确认无法恢复任务；已保存回执的精确重试仅返回历史回执。
执行中的取消竞争、故障恢复与 SSE 重连另行提供。
