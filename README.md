# Trainable Tool Agent Lab

模拟售后业务的数据、共享契约、应用配置、SQLite 存储与源码打包工具。数据和契约预览命令只读取样例，不执行退款或启动 Agent。

版本化政策位于 `data/business/v1/policies.json`，共 24 份，涵盖当前规则、品类差异及旧版/未来版对照；来源规格与引用方式见[业务数据说明](data/business/v1/README.md#政策文档与引用)。政策一致性检查：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_policy_data.py
```

## 本地轻量环境

使用 Python 3.12 和 uv；应用依赖为 Pydantic、PyYAML、python-dotenv，测试使用 pytest，不需要模型推理或训练依赖。

```powershell
uv sync --locked --cache-dir .uv-cache
uv run --no-sync --cache-dir .uv-cache python -m pytest
```

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

## 任务与人工确认

`TaskService` 创建和查询带归属的任务、启动当前尝试并保存版本化提案；
`ApprovalService` 绑定当前提案的完整参数快照，记录批准或拒绝。
同一确认请求重试返回原记录，参数改变后须确认新版本。
接口和临时库退款例子见[任务与确认说明](docs/tasks_and_approvals.md)。

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/integration/test_approvals.py
```

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
python scripts/package_project.py --dry-run
python scripts/package_project.py
python scripts/package_project.py --output-dir artifacts/delivery
```

默认在 `artifacts/packages/` 生成源码 ZIP 与 `.zip.sha256`，输出文件数、体积及校验值。预览列出包含文件和排除原因；被排除的目录只列目录本身，不遍历其内部。相对输出目录始终以项目根目录为基准，从其他工作目录调用脚本结果一致。

规则位于 `deploy/package_rules.toml`。收集符合规则的所有当前文件，包含尚未提交的新文件；排除环境、缓存、数据库、密钥命名文件、权重、构建产物和历史包。规则只管理文件范围，不分析源码中是否误写了凭据；示例配置应仅含无密钥的示例值。

`frontend/` 存在时须同时提供 `package.json` 和 npm 的 `package-lock.json`；`training/art/` 存在时须提供其 `pyproject.toml` 和真实 `uv.lock`。缺失时打包失败。

包内 `PACKAGE_MANIFEST.json` 包含每个源码文件的大小和 SHA-256、内容版本、规则版本及可用的 Git 信息；清单不递归计算自身哈希。上传与解压更新见 [部署说明](deploy/README.md)。

Protected runtime tool calls: see [ToolExecutor usage](docs/executor.md).
