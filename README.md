# Trainable Tool Agent Lab

可训练的售后工单 Agent 工作台。当前处于 M1，已实现独立 Python 环境、应用配置、最小业务数据检查和源码打包工具；尚无业务执行、API、MCP、页面或模型训练。

## 本地轻量环境

在本目录使用 Python 3.12 和 uv；应用配置使用 Pydantic、PyYAML、python-dotenv，开发依赖为 pytest。本地不安装 torch、ART 或模型推理栈。

```powershell
uv sync --locked --cache-dir .uv-cache
uv run --no-sync --cache-dir .uv-cache python -m pytest
```

若本机只有其他 Python 版本，可将独立解释器放到已忽略的项目产物目录，不改变系统 Python：

```powershell
uv python install 3.12 --no-bin --no-registry --install-dir artifacts/tooling/python --cache-dir .uv-cache
uv sync --locked --python artifacts/tooling/python/cpython-3.12.13-windows-x86_64-none/python.exe --cache-dir .uv-cache
```

第二条命令中的解释器目录使用上一条命令实际安装的版本；这里记录本次 Windows 使用的 3.12.13 路径。

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
| 运行标签 | `TTAL_MODE` | `manual`，也接受 `mock`；尚未实现业务执行入口 |
| 开发身份 | `TTAL_DEV_OWNER_ID` | `demo-user` |
| 运行目录 | `TTAL_RUNTIME_DIR` | `artifacts/runtime` |
| 模拟业务资源目录 | `TTAL_BUSINESS_DATA_DIR` | `data/business/v1`，已包含 M1-004 的最小规格与样例 |

相对路径统一以源码项目根目录为基准，与当前工作目录、配置文件所在目录无关。`--config` 和 `--env-file` 可选择其他文件，也遵循这一规则；绝对路径保持自身位置。指定文件不存在时直接报错。

应用数据库路径为 `<runtime_dir>/app.sqlite3`，checkpoint 路径为 `<runtime_dir>/checkpoints.sqlite3`，日志目录为 `<runtime_dir>/logs`。`--init-dirs` 只创建运行和日志目录，业务数据库初始化留给 M1-008。Linux 服务器可通过 `TTAL_RUNTIME_DIR=/srv/tool-agent-lab/state` 将运行数据放到源码版本目录之外；该服务器路径尚未实跑。

后续 API、worker 和 MCP 入口共用 `from tool_agent_lab.settings import load_settings`。本模块的针对性检查为：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_settings.py
```

## 最小业务数据与预期结果预览

[业务规格](data/business/v1/README.md) 包含 4 个订单、1 笔附确认快照的历史退款和 5 个样例，覆盖规则退款、退款加补偿、核对不确定结果、澄清/转人工四类任务。固定业务时间为 `2026-09-17T12:00:00+08:00`，CNY 金额使用整数分。

```powershell
uv run --no-sync --cache-dir .uv-cache python scripts/check_business_data.py
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_business_data.py
```

第一条命令只读取应用配置指向的 `business_data_dir`，检查金额、引用和初始退款账本，输出手工期望及 `business_execution: not_run`；不会创建数据库、执行退款或调用模型。每个样例须独立重置初始状态，期望答案不提供给模型。20–40 份政策和约 30 个边界案例将在后续补齐。

## 源码打包

激活 Python 3.12 或直接指定其路径即可；脚本仅用标准库，不要求安装项目、uv 或 Git。

```powershell
python scripts/package_project.py --dry-run
python scripts/package_project.py
python scripts/package_project.py --output-dir artifacts/delivery
```

默认在 `artifacts/packages/` 生成源码 ZIP 与 `.zip.sha256`，输出文件数、体积及校验值。预览列出包含文件和排除原因；被排除的目录只列目录本身，不遍历其内部。相对输出目录始终以项目根目录为基准，从其他工作目录调用脚本结果一致。

规则位于 `deploy/package_rules.toml`。收集符合规则的所有当前文件，包含尚未提交的新文件；排除环境、缓存、数据库、密钥命名文件、权重、构建产物和历史包。规则只管理文件范围，不分析源码中是否误写了凭据；示例配置应仅含无密钥的示例值。

前端和训练子项目尚未创建。加入 `frontend/` 后必须同时提供 `package.json` 和 npm 的 `package-lock.json`；加入 `training/art/` 后必须提供其 `pyproject.toml` 和在服务器生成的真实 `uv.lock`。缺失时打包失败，不静默漏掉锁文件。

包内 `PACKAGE_MANIFEST.json` 包含每个源码文件的大小和 SHA-256、内容版本、规则版本及可用的 Git 信息；清单不递归计算自身哈希。上传与解压更新见 [部署说明](deploy/README.md)。

进度见 [进度记录](reports/progress.md) 和 [M1 局部验证](reports/M1/acceptance.md)。完整退款流程及其确认、账本和重复写验证将在后续 M1 工作中完成。
