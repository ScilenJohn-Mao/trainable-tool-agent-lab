# Trainable Tool Agent Lab

模拟售后业务工作台，提供浏览器工单页面、订单与政策查询、人工提案/确认、受保护的 MCP 退款/补偿/转人工、SQLite 账本和 HTTP 任务接口。使用模拟订单、固定业务时间及 CNY 整数分。

## 启动浏览器工作台

准备 Python 3.12、uv 和 Node.js 22.12 或更新版本。首次安装从项目根目录执行；Python 命令只安装轻量应用依赖：

```powershell
uv sync --locked --python 3.12 --cache-dir .uv-cache
npm --prefix frontend ci
uv run --no-sync --cache-dir .uv-cache python scripts/seed_demo.py --database artifacts/runtime/workbench/app.sqlite3
```

初始化命令要求目标数据库不存在，不会覆盖已有记录。`workbench` 是此次演示的独立运行目录，保留其中的数据库与 checkpoint 可继续查看和处理原工单。

打开三个终端。前两个终端均在项目根目录运行，并设置相同运行目录与模型配置。

终端一启动 API：

```powershell
$env:TTAL_RUNTIME_DIR="artifacts/runtime/workbench"
$env:TTAL_MODEL_CONFIG_FILE="configs/models/mock.yaml"
uv run --no-sync --cache-dir .uv-cache python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

终端二启动 worker：

```powershell
$env:TTAL_RUNTIME_DIR="artifacts/runtime/workbench"
$env:TTAL_MODEL_CONFIG_FILE="configs/models/mock.yaml"
uv run --no-sync --cache-dir .uv-cache python -m apps.worker.main --mock-responses data/scenarios/demo_refund_replies.json
```

终端三启动页面：

```powershell
cd frontend
npm run dev
```

访问 [工单工作台](http://127.0.0.1:5173)。页面通过同机 API 查询与提交工单，SSE 展示执行进展；Vite 将 `/api` 代理到 `http://127.0.0.1:8000`。更换 API 地址时，在页面终端设置 `TTAL_API_TARGET` 后再启动。下载依赖后，页面不加载外部字体或 CDN 资源。

人工测试步骤：

1. 输入“收到的商品已损坏，请处理售后”，先留空订单号，创建工单。
2. 在补充信息面板回复 `ORD-1001`。核对方案显示订单、退款 **12900 分 / ¥129.00**、理由和提案版本。
3. 点击“确认执行”，核对业务回执中的实际退款金额与操作键，展开政策依据查看原文、版本和条款。右侧可展开工具参数和结果。
4. 刷新浏览器，原工单、等待点和最终结果仍可查看。也可在等待补充信息或确认时取消工单。
5. 测试拒绝时使用另一份新数据库：停止 API 与 worker，将上述命令中的 `workbench` 换为 `workbench-reject`，先初始化，再启动两者。点击“拒绝方案”，结果应无业务操作与退款。

示例 worker 使用固定的 **脚本 mock**，每条工单都演示 ORD-1001 的同一退款流程，页面明确显示模型标识。它用于测试界面与业务连接，不会根据任意问题智能选择订单。确认后的真实模拟业务记录不会自动重置，同一订单不能再次退款；新一轮演示请另选运行目录。API、worker、页面各用 Ctrl+C 停止。

Linux 的步骤相同，环境变量用 shell 写法，例如 `export TTAL_RUNTIME_DIR=artifacts/runtime/workbench` 和 `export TTAL_MODEL_CONFIG_FILE=configs/models/mock.yaml`；其余命令保持一致。

使用真实本机模型时，先按下文准备模型与独立 `inference/` 环境。API 与 worker 均将 `TTAL_MODEL_CONFIG_FILE` 改为 `configs/models/qwen3b-local.yaml`，使用另一份已初始化的运行目录；API 仍在轻量环境运行，worker 命令改为：

```powershell
uv run --project inference --no-sync --cache-dir .uv-cache python -m apps.worker.main
```

此时不传 `--mock-responses`。页面操作保持一致，模型直接在 worker 中读取本机权重；真实模型可能产生不同的澄清问题、方案或失败结果，金额与业务结果仍以受保护的服务和账本为准。

`qwen3b-local.yaml` 为完整工单设置 8192 token 总上下文、最多 512 输出。共享工具契约、订单/政策和确认快照也占上下文；单次简短工具请求可在 4K 内运行，完整流程需要更大预算。输入加输出超限会明确失败，不会静默删除确认事实。实际显存取决于上下文和 GPU。

共享 Agent 另按 `configs/budgets.yaml` 将模型可见上下文控制在 24000 UTF-8 字节、单条工具输出控制在 2048 字节；完整工具结果和引用仍保存在运行状态中。较早的非关键历史可按完整工具调用/结果对移出模型上下文，订单事实、确认和操作记录保留。长输入或反复补充仍可能超限，请优先使用短而明确的工单。

生产构建使用 `npm --prefix frontend run build`；本机查看构建结果可在 `frontend/` 执行 `npm run preview`，API 与 worker 仍须启动。浏览器回归需要先安装前端依赖；Windows 使用已安装的 Edge，Linux 先执行 `npx playwright install chromium`。从项目根目录执行：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/e2e/test_ticket_flow.py
```

该测试自动启动隔离 API、worker 和页面，验证批准、拒绝、取消、刷新及窄屏布局，不接现有业务库。`npm --prefix frontend run test:e2e` 则使用已启动的服务，默认地址为 `http://127.0.0.1:5173`。

## 通过 API 演示 Agent

先按上面的步骤使用新运行目录初始化数据库，并启动 API 与 worker。无需启动前端，另开终端在项目根目录执行：

```powershell
uv run --no-sync --cache-dir .uv-cache python scripts/demo_agent.py --output artifacts/runtime/agent-receipt.json
```

脚本创建一个未提供订单号的到货损坏工单，在实际澄清等待点补充 `ORD-1001`，展示 worker 产生的完整提案，然后等待输入 `approved` 或 `rejected`。没有有效的操作者决定不会批准。也可由操作者在命令中明确选择首次提案的决定：

```powershell
uv run --no-sync --cache-dir .uv-cache python scripts/demo_agent.py --decision approved --output artifacts/runtime/agent-approved.json
uv run --no-sync --cache-dir .uv-cache python scripts/demo_agent.py --decision rejected --output artifacts/runtime/agent-rejected.json
```

批准与拒绝示例应分别使用新数据库；固定 mock 演示的 ORD-1001 只能退款一次。`--base-url` 指向应用 API，默认 `http://127.0.0.1:8000`，不是模型服务地址。脚本不加载模型、不直接访问业务库，也不启动 API 或 worker；同一脚本可配合本机真实模型 worker，模型行为由运行中的 worker 决定。

stdout 与可选输出文件是 UTF-8 JSON，包含实际模型/配置版本、工单/图线程 ID、补充输入和确认回执、状态及 API 返回的最终结果。`task.result.operations` 中的金额和操作键才表示已提交业务；`finish_reason=completed` 仅表示工单有最终结果，模型也可能只完成事实核对而没有退款。

默认轮询预算为 60 秒，人工输入等待不计入，可用 `--timeout 180` 调整。失败、超时、没有操作者输入或再次要求补充/确认时返回非零退出码，并保存当前可见状态；不取消工单、不重发写请求、不自动批准额外提案。HTTP/文件错误打印到 stderr；创建成功时 stderr 会先给出工单 ID，后续可从工作台核对。

## 快速运行

以下命令从项目根目录执行。先准备下文的 Python 3.12 轻量环境，再运行手工退款演示：

```powershell
uv run --no-sync --cache-dir .uv-cache python scripts/demo_business.py
```

脚本每次创建新的隔离业务库，查 ORD-1001、检索并回读政策、展示完整提案。操作者输入 `approved` 或 `rejected`；没有有效输入就不会批准。批准后可核对 stdout JSON：`status=refunded`、`task.status=completed`、`operation.amount_minor=12900`、原键查询与重复调用标志均为 true。12900 分等于 129 元；数据库路径在 `database` 中，业务记录保留在该库。

非交互运行由操作者明确选择本次决定，仍展示提案并经过确认服务：

```powershell
uv run --no-sync --cache-dir .uv-cache python scripts/demo_business.py --decision approved
uv run --no-sync --cache-dir .uv-cache python scripts/demo_business.py --decision rejected
```

拒绝输出 `status=rejected`、`operation=null`，订单金额不变。提案和提示在 stderr，结果 JSON 在 stdout；这是固定订单的手工工具流程。完整命令和错误语义见[退款演示](docs/business_demo.md)。

需要 HTTP 时按下文[HTTP 任务与确认服务](#http-任务与确认服务)启动；源码上传、服务器启动和版本切换见[部署说明](deploy/README.md)。HTTP 创建、补充输入和人工确认已对接独立 worker，API 与 worker 需使用相同运行目录及模型/Agent 配置；用法见[worker 说明](docs/worker.md)。

版本化政策位于 `data/business/v1/policies.json`，共 24 份，涵盖当前规则、品类差异及旧版/未来版对照；来源规格与引用方式见[业务数据说明](data/business/v1/README.md#政策文档与引用)。政策一致性检查：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_policy_data.py
```

## 本地轻量环境

轻量环境用于 mock 和兼容 HTTP 模型端点；本地权重直接加载使用下文的独立推理环境：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.agent.model_client --message "Hello"
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.agent.model_client --config configs/models/qwen3b.yaml
```

第一条展示带 `mock-v1` 标识的回复，第二条只查看 3B 配置，不连接服务器。
实际端点、工具调用和 Python 接口见[模型客户端说明](docs/models.md)。此入口不执行工单或退款。

使用 Python 3.12 和 uv；应用依赖为 FastAPI、Uvicorn、HTTPX、MCP、Pydantic、PyYAML、python-dotenv，测试使用 pytest，不需要模型推理或训练依赖。

首次创建或锁文件变化时同步轻量应用环境；已有可用环境直接使用 `--no-sync` 命令：

```powershell
uv sync --locked --python 3.12 --cache-dir .uv-cache
uv run --no-sync --cache-dir .uv-cache python -c "import sys; print(sys.version); print(sys.executable)"
```

应显示 Python 3.12 和本项目 `.venv` 中的解释器。也可将 `--python` 改成已安装 Python 3.12 的绝对路径。此处只同步轻量应用锁文件，不安装模型推理或训练依赖。

## 本地模型文件

基座模型和 LoRA adapter 统一放在项目根目录的 `models/`，Windows 和 Linux 使用相同的相对目录约定：

```text
trainable-tool-agent-lab/
  models/
    base/
      Qwen2.5-3B-Instruct/
        config.json
        tokenizer_config.json
        tokenizer.json
        model*.safetensors
        ...
    adapters/
      <version>/
        adapter_config.json
        adapter_model.safetensors
        ...
  configs/models/
```

从 [Qwen 官方模型文件页面](https://huggingface.co/Qwen/Qwen2.5-3B-Instruct/tree/main)下载同一 revision 的完整仓库文件到 `models/base/Qwen2.5-3B-Instruct/`；上面的文件列表只是示意，应保留仓库内实际需要的配置、tokenizer、权重分片及索引。不要仅下载一片权重，也不要放入 Git LFS 指针文件。训练后导出的标准 PEFT adapter 放到 `models/adapters/<version>/`，并使用与训练一致的基座 revision、tokenizer 和聊天模板。

`models/` 保存模型资产，包括 Hugging Face 的模型配置；`configs/models/` 保存应随源码交付的应用模型选择与生成参数。根目录 `/models/` 被 Git 忽略，并在源码包白名单之外；`configs/models/` 仍须打包。模型资产单独复制或下载，源码更新时保留已有模型目录。`artifacts/` 继续保存业务运行数据、日志和源码包，无需把模型放到 `artifacts/models/`。

应用的直接推理方式采用 Transformers + PEFT，在每台机器的 worker 内加载本机文件；不要求远程模型服务、服务器 IP、Ollama 或 vLLM。以 3B 的 4-bit、单请求、2K–4K 总上下文及最多 512 个输出 token 为初始验证配置，实际显存和工具效果须实测。模型、依赖及前端资源准备齐全后，加载使用本地路径与离线选项；浏览器到同机 API 的 HTTP/SSE 不需要外网。

客户端支持 `local`、`mock` 和兼容 HTTP 三种入口；`local` 直接加载本机基座与可选 PEFT adapter，不调用模型 HTTP。当前可通过 CLI 调用，共享 Agent 图与独立 worker 已可调用该接口，页面已通过应用 API 接入；启动方法见[worker 说明](docs/worker.md)。真实 3B/GPU 与训练 adapter 的两端效果仍须实测；轻量测试中的模型替身不能代替它们。完整参数与消息历史用法见[模型客户端说明](docs/models.md)。

在项目根目录准备独立推理环境（Windows，使用已有 Python 3.12 解释器）：

```powershell
uv sync --project inference --locked --python .venv/Scripts/python.exe --cache-dir .uv-cache
uv run --project inference --no-sync --cache-dir .uv-cache python -m tool_agent_lab.agent.model_client --config configs/models/qwen3b-local.yaml --message "请查询 ORD-1001 的订单信息" --with-tools
```

Linux 使用同一锁文件和调用命令，安装时将 `--python .venv/Scripts/python.exe` 改为 `--python python3.12`。锁文件限定 Python 3.12、Windows AMD64/Linux x86_64 和 PyTorch CUDA 12.8 wheel；驱动与 GPU 运行兼容需在各机器验证。安装会下载依赖，提前准备好完整模型与依赖后，推理只读取本地模型文件。缺文件、缺依赖或 CUDA 不可用直接报错，不切换到远程模型或 mock。CLI 只输出模型回复/工具请求，不执行工具或退款。

推理环境使用独立的 Python 3.12/uv 环境，安装 PyTorch、Transformers、PEFT、Accelerate、bitsandbytes 及共享应用包；轻量 `.venv` 保持原有用途。两端共用应用源码，依赖包按 Windows/Linux 和 GPU 匹配。RL 训练使用 Linux 的独立 `training/art/` 环境，并依赖固定版本 ART；其 vLLM runtime 按对应 ART 的 `vllm_runtime/pyproject.toml`、`uv.lock` 和 `setup.sh` 创建独立 uv 环境，不能把裸 vLLM 安装到应用或训练主环境替代它。ART 内部允许同机通信；训练及日志保存到本机，训练后的 adapter 通过文件搬回 Windows 推理，Windows 无需安装 ART 或 vLLM。具体环境安装命令须以对应版本的锁文件与实际验证为准。

## 应用配置与运行目录

查看解析后的配置和绝对路径，或者显式创建运行目录：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.settings
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.settings --init-dirs
```

默认读取 `configs/app.yaml` 和可选的项目根目录 `.env`。优先级为进程环境变量 > `.env` > YAML；可从 `.env.example` 复制所需配置。读取只合并值，不修改进程、用户或系统环境变量。`.env` 使用字面值，不展开 `${VARIABLE}`；路径支持 `~`。

| 字段 | 环境变量 | 默认值 |
|---|---|---|
| 配置版本 | `TTAL_CONFIG_VERSION` | `app-v1` |
| 运行标签 | `TTAL_MODE` | `manual`，也接受 `mock` |
| 开发身份 | `TTAL_DEV_OWNER_ID` | `demo-user` |
| 运行目录 | `TTAL_RUNTIME_DIR` | `artifacts/runtime` |
| 模拟业务资源目录 | `TTAL_BUSINESS_DATA_DIR` | `data/business/v1` |
| 模型配置文件 | `TTAL_MODEL_CONFIG_FILE` | `configs/models/mock.yaml` |
| Agent 配置文件 | `TTAL_AGENT_CONFIG_FILE` | `configs/agents/default.yaml` |

相对路径统一以源码项目根目录为基准，与当前工作目录、配置文件所在目录无关。`--config` 和 `--env-file` 可选择其他文件，也遵循这一规则；绝对路径保持自身位置。指定文件不存在时直接报错。

应用数据库路径为 `<runtime_dir>/app.sqlite3`，checkpoint 路径为 `<runtime_dir>/checkpoints.sqlite3`，日志目录为 `<runtime_dir>/logs`。`--init-dirs` 只创建运行和日志目录，不创建数据库。通过 `TTAL_RUNTIME_DIR` 可以将运行数据放到源码目录之外。

Python 调用入口为 `from tool_agent_lab.settings import load_settings`。配置检查命令：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_settings.py
```

## 应用数据库初始化

```powershell
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.storage.database
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.storage.database --database artifacts/runtime/demo.sqlite3
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_database.py
```

默认初始化配置中的 `app_db_path`；`--database` 指定的相对路径以当前工作目录为基准。命令创建父目录和空应用表，输出绝对路径及 schema 版本，重复运行保留已有数据。checkpoint 数据库单独管理。表、事务、唯一约束及 Python 接口见[存储说明](docs/storage.md)。

`TaskRepository`、`BusinessRepository`、`EventRepository` 提供带归属的读取、版本化提案/确认、订单与账本写入、事件追加及游标续读，复用调用方事务。验证这些存储接口：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/integration/test_repositories.py
```

## 隔离演示库

```powershell
uv run --no-sync --cache-dir .uv-cache python scripts/seed_demo.py
uv run --no-sync --cache-dir .uv-cache python scripts/seed_demo.py --database artifacts/demo/refund-example/app.sqlite3
```

默认创建独立的 `artifacts/demo/app.sqlite3`，载入四个订单并通过现有确认/业务服务重建历史退款。已有目标与应用/checkpoint 库均拒绝，不覆盖已有业务。路径、历史回执和 HTTP 使用方式见[演示库说明](docs/demo_database.md)。

## 手工退款演示

```powershell
uv run --no-sync --cache-dir .uv-cache python scripts/demo_business.py
uv run --no-sync --cache-dir .uv-cache python scripts/demo_business.py --decision approved
```

每次使用新的隔离库，查询订单/政策、展示提案，由操作者明确批准或拒绝。批准经同一确认服务与可信执行器调用真实 MCP，核对 12900 分退款、原操作键与重复调用；默认不自动批准。命令、结果和失败语义见[退款演示说明](docs/business_demo.md)。

## 最小业务数据与预期结果预览

[业务规格](data/business/v1/README.md) 包含 4 个订单、1 笔附确认快照的历史退款和 5 个样例，覆盖规则退款、退款加补偿、核对不确定结果、澄清/转人工四类任务。固定业务时间为 `2026-09-17T12:00:00+08:00`，CNY 金额使用整数分。

```powershell
uv run --no-sync --cache-dir .uv-cache python scripts/check_business_data.py
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_business_data.py
```

第一条命令只读取应用配置指向的 `business_data_dir`，检查金额、引用和初始退款账本，输出手工期望及 `business_execution: not_run`；不会创建数据库、执行退款或调用模型。每个样例须独立重置初始状态，期望答案不提供给模型。

## 政策读取与检索

`PolicyCatalog` 导入版本化政策并按 ID/版本/section 读取原文；`PolicySearch` 按
固定业务时间建立内存 BM25 条款索引，返回带版本、有效期和位置的证据。
支持品类、版本与条数限制，中文采用字符及相邻双字切分，不需要模型或额外依赖。
接口、完整文档读取与历史版本语义见[政策说明](docs/policies.md)。

```powershell
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.knowledge.search search "延迟补偿券固定500分" --category general_goods --limit 3
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.knowledge.search read P-DELAY-AMOUNT mock-policy-v1 --section amount
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_policy_search.py
```

## 业务规则资格检查

`BusinessRules` 按模拟规格判断退款、延迟补偿和转人工的资格与金额，返回结构化原因；默认使用固定业务时间，检查政策生效区间及引用。它只做纯规则判断，不执行退款、消费确认或写数据库。调用示例见[业务规则说明](docs/business_rules.md)。

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_business_rules.py
```

[32 个独立边界案例](data/cases/README.md)保存完整订单/动作输入及手工期望，覆盖金额、时间、澄清、人工和政策依据。只读核对与受保护写入检查可分别运行：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_boundary_cases.py -p no:cacheprovider
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/integration/test_tool_executor.py -p no:cacheprovider
```

前者不写数据库，后者使用隔离库和真实 MCP，验证确认、正确金额、期限复核、原键重试、重复写保护和事务回滚。

## 任务与人工确认

`TaskService` 创建和查询带归属的任务、启动当前尝试并保存版本化提案；
`ApprovalService` 绑定当前提案的完整参数快照，记录批准或拒绝。
同一确认请求重试返回原记录，参数改变后须确认新版本。
接口和临时库退款例子见[任务与确认说明](docs/tasks_and_approvals.md)。

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/integration/test_approvals.py
```

## HTTP 任务与确认服务

通过本地 HTTP 创建工单、查询任务/列表和完整当前提案，提交补充输入、批准/拒绝，并取消排队或等待人工交互的任务。详情包含当前尝试身份、待回答问题和已保存的执行结果；`/tasks/{task_id}/events` 通过 SSE 按序反馈持久化进展，等待人工交互时保持连接，终态排空后关闭。
身份由服务端配置绑定，确认复用同一 ApprovalService，不直接执行业务。
启动、请求字段、错误码和使用示例见[HTTP 接口说明](docs/http_api.md)。

```powershell
uv run --no-sync --cache-dir .uv-cache python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

该命令使用当前配置的应用库，启动时初始化空表或保留已有数据；默认初始化不载入演示订单。若要使用隔离的四订单库，在第一个 PowerShell 终端执行：

```powershell
$demoRuntime = "artifacts/demo/http-$([guid]::NewGuid().ToString('N'))"
uv run --no-sync --cache-dir .uv-cache python scripts/seed_demo.py --database "$demoRuntime/app.sqlite3"
if ($LASTEXITCODE -ne 0) { throw 'Demo database initialization failed' }
$previousRuntime = $env:TTAL_RUNTIME_DIR
try {
    $env:TTAL_RUNTIME_DIR = $demoRuntime
    uv run --no-sync --cache-dir .uv-cache python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
} finally {
    $env:TTAL_RUNTIME_DIR = $previousRuntime
}
```

先初始化，再指定运行目录，使用默认 `demo-user` 身份；退出服务用 Ctrl+C，finally 恢复该终端先前的变量。已有目标不会被 seed 覆盖。服务运行后，在第二个终端创建并查询任务：

```powershell
$base = 'http://127.0.0.1:8000'
$task = Invoke-RestMethod -Method Post -Uri "$base/tasks" -ContentType 'application/json' -Body '{"user_message":"Damaged order, please review","order_id":"ORD-1001"}'
Invoke-RestMethod -Uri "$base/tasks/$($task.task_id)"
Invoke-RestMethod -Uri "$base/tasks?status=queued&limit=10"
Invoke-RestMethod -Uri "$base/tasks/$($task.task_id)/proposal"
```

创建返回 queued；独立 worker 使用相同配置领取任务，处理到等待点或终态。没有运行时发布提案时 proposal 为 null。浏览器打开 `http://127.0.0.1:8000/docs`，OpenAPI 在 `/openapi.json`。HTTP 输入/批准仅记录回执，业务执行由 worker 的共享图与受保护执行器完成；字段、取消边界与重试语义见[接口说明](docs/http_api.md)。

## 受保护业务写入

`BusinessService.prepare` 在工具调用前持久化 pending 操作键，重复准备复用原记录；
`execute` 重新读取任务、提案、批准和操作键，核对归属、参数、版本、有效期和业务规则，
将确认消费、订单变更与操作账本同事务提交。`get_operation` 按原键查询带归属的真实
账本；换键或新批准不能重复退款/补偿。
Python 接口及重复写入行为见[业务执行说明](docs/business_service.md)。

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/integration/test_business_transactions.py
```

测试在隔离库通过存储接口准备合成确认，验证真实模拟业务写入；不是完整用户确认
入口或工具调用演示。

## 共享契约预览

[契约说明](docs/contracts.md)定义任务/尝试身份、七个状态、退款/补偿/转人工参数、提案快照、批准或拒绝、运行时执行上下文和事件。

```powershell
uv run --no-sync --cache-dir .uv-cache python scripts/check_contracts.py
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_schemas.py
```

预览使用 ORD-1001 的 12900 分退款样例，验证 8 种记录的 JSON 往返。确认记录为合成样例，输出 `approval_service: not_run`、`business_execution: not_run`；命令只校验结构，不调用确认服务、执行退款或写入数据库。

## 工具契约与 Schema 导出

七工具的模型参数、带 call_id 的结构化成功/错误结果及 JSON Schema 位于
`tool_agent_lab.tools.contracts`。写工具复用业务动作参数，成功结果要求已提交账本；
不确定错误保留原操作键。接口见[工具契约说明](docs/tool_contracts.md)。

```powershell
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.tools.contracts
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_tool_contracts.py
```

导出命令只输出七工具的 schema，不启动 MCP 会话或调用工具。

## 本地 MCP stdio

客户端通过真实子进程握手、列出七工具并调用四个只读工具，保留原始和类型化结果。
本地身份在启动时绑定；三种写工具在缺少可信执行绑定时返回结构化拒绝。
安装、Windows JSON 输入和 Python 接口见[MCP 使用说明](docs/mcp.md)。

```powershell
uv sync --locked --cache-dir .uv-cache
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.tools.client list
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/integration/test_mcp_stdio.py
```

## 源码打包

激活 Python 3.12 或直接指定其路径即可；脚本仅用标准库，不要求安装项目、uv 或 Git。

```powershell
.\.venv\Scripts\python.exe -I -S scripts/package_project.py --dry-run
.\.venv\Scripts\python.exe -I -S scripts/package_project.py
.\.venv\Scripts\python.exe -I -S scripts/package_project.py --output-dir artifacts/delivery
```

默认在 `artifacts/packages/` 生成源码 ZIP 与 `.zip.sha256`，输出文件数、体积及校验值。预览列出包含文件和排除原因；被排除的目录只列目录本身，不遍历其内部。相对输出目录始终以项目根目录为基准，从其他工作目录调用脚本结果一致。

规则位于 `deploy/package_rules.toml`。收集符合规则的所有当前文件，包含尚未提交的新文件；排除环境、缓存、数据库、密钥命名文件、权重、构建产物和历史包。规则只管理文件范围，不分析源码中是否误写了凭据；示例配置应仅含无密钥的示例值。

`frontend/` 存在时须同时提供 `package.json` 和 npm 的 `package-lock.json`；`training/art/` 存在时须提供其 `pyproject.toml` 和真实 `uv.lock`。缺失时打包失败。

包内 `PACKAGE_MANIFEST.json` 包含每个源码文件的大小和 SHA-256、内容版本、规则版本及可用的 Git 信息；清单不递归计算自身哈希。上传与解压更新见 [部署说明](deploy/README.md)。

可信运行时如何发布提案并执行批准动作，见[执行器接口](docs/executor.md)。
