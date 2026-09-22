# M1 局部验证

日期：2026-09-17。M1 尚未整阶段验收。

已交付轻量环境、应用配置、最小业务规格与数据检查、源码打包，本轮新增 M1-004，累计 9/31 项。业务工具、确认服务、数据库账本、边界案例和完整退款演示尚未实现，M1-031 保持未完成。下文按任务保留历史证据。

## 已通过的本地轻量检查

执行来源：Codex；Windows PowerShell、uv 0.10.9、项目内 CPython 3.12.13；以下命令工作目录为 `trainable-tool-agent-lab/`。

| 任务 | 实际命令或证据 | 结果 |
|---|---|---|
| M1-001 | `uv sync --python artifacts/tooling/python/cpython-3.12.13-windows-x86_64-none/python.exe --cache-dir .uv-cache` | 已生成真实 `uv.lock` 并建立独立 `.venv`；安装项目和 6 个轻量测试依赖 |
| M1-001 | `uv sync --locked --offline --cache-dir .uv-cache` | 调整 pytest 搜索目录后离线重建本项目的 editable 安装，依赖版本未变 |
| M1-001 | `uv sync --locked --check --offline --cache-dir .uv-cache`；`artifacts/M1/environment.json` | 锁文件一致，无待同步变更；导入指向本项目；无 torch |
| M1-003 | `reports/progress.md` | 按任务编号记录范围、证据、本地/服务器状态及下一步 |
| M1-021/022/023/027 | `uv run --no-sync --cache-dir .uv-cache python -m pytest -q` | 权限修复批次 **12 passed in 2.55s**，无跳过项；本轮未重跑 |
| M1-002 | `uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_settings.py` | M1-002 批次 **9 passed in 0.93s**，无跳过项；本轮未重跑 |
| M1-004 | `uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_business_data.py` | 本轮 **9 passed in 0.48s**，无跳过项 |

测试实际覆盖：白名单资源与锁文件、嵌套虚拟环境/缓存/运行产物排除、Windows 外部 junction、无 Git 和真实 Git 未提交文件、任意工作目录、标准库独立 CLI、相对路径与单一顶层目录、逐文件/整包 SHA-256、解压后预览、自定义输出/历史包排除、必需锁文件缺失及读写失败不发布残包。首轮一次失败来自测试样本的 Windows 换行转换，已固定样本换行后通过；源码按原始字节保留。

## M1-002 配置模块（2026-09-17）

新增 `settings.py`、`configs/app.yaml`、无密钥 `.env.example` 及 `test_settings.py`。YAML 提供基线，`.env` 和 `TTAL_*` 进程变量依次覆盖；只读取值，不改写环境变量。路径锚定源码根目录，可将运行目录指向外部持久位置；应用库与 checkpoint 路径分开，读取配置没有文件写入。

9 项针对性测试覆盖优先级、跨工作目录、外部路径、显式初始化、不创建数据库、非法 mode/空身份/拼错字段及指定文件缺失。实际命令 `.venv/Scripts/python.exe -m tool_agent_lab.settings --init-dirs` 创建 `artifacts/runtime/logs/` 并输出解析结果，未创建数据库。实际命令、依赖版本和输出为 `artifacts/M1/settings-check.json`。

本轮先用 `uv lock --cache-dir .uv-cache` 生成锁文件，读取包清单确认不含重依赖，再用 `uv sync --locked --cache-dir .uv-cache` 安装 Pydantic 2.13.5、PyYAML 6.0.3、python-dotenv 1.2.3 及其轻量依赖。沿用项目 Python 3.12.13，没有修改系统 Python 或环境变量。

打包规则将配置模块、默认 YAML 和 `.env.example` 列为必需文件，`.gitignore` 沿用已有排除规则。旧打包测试没有重跑；新交付包只核对新增资源与更新锁文件、清单/权限及解压后配置入口。实现参考：[Pydantic 配置](https://docs.pydantic.dev/latest/api/config/)、[PyYAML](https://pyyaml.org/wiki/PyYAMLDocumentation)、[python-dotenv](https://bbc2.github.io/python-dotenv/)。

## Windows 权限回归（2026-09-17）

用户反馈两个 ZIP 在 WinRAR 中报 `Access denied`。实际 ACL 显示 ZIP/校验文件关闭继承，仅所有者、SYSTEM 和管理员可访问；同目录验证 JSON 正常继承。原因是 `TemporaryDirectory` 创建的私有 ACL 随文件移动保留，最初 11 项测试未覆盖不同账号读取。

- 仅对两份历史 ZIP 及对应 `.sha256` 共四个文件逐一执行 `icacls <文件完整路径> /reset`，恢复父目录默认权限；修复前后 SHA-256 一致。权限前后快照保存在 `artifacts/M1/permissions-repair.json`。
- 用户桌面验证：原有 ZIP 经正常双击后，用户回复“现在能正常打开”。此项由用户执行，非 Codex 操作 WinRAR 界面。
- 新增 `test_delivery_files_inherit_output_read_permissions`，为测试输出目录授予普通 Users 组可继承的读取权限，检查两份交付文件不关闭继承且确实继承读取授权。旧实现实跑为 **1 failed**，修复后完整套件 **12 passed**。
- `package_project.py` 改用普通唯一暂存目录，生成完成后移动并清理自己创建的两个暂存文件；无需生产脚本调用权限修改命令。现有失败退出和残包清理检查继续通过。
- 实际新包经 `artifacts/M1/verify_delivery.py` 验证，交付摘要的 `windows_output_permissions` 记录 ZIP 与校验文件均正常继承且普通 Users 组可读取。

## 实际源码包

M1-030 已通过实际项目的预览、生成与本地新目录解压检查。执行命令：`.venv/Scripts/python.exe artifacts/M1/verify_delivery.py`；该本地证据脚本依次用 `-I -S` 调用 `scripts/package_project.py --dry-run` 和 `scripts/package_project.py`，用标准库核对 ZIP CRC、逐文件/整包 SHA-256、单一顶层目录、预览一致性，然后解压并运行归档中的 CLI 和源码导入；Windows 下另检查交付文件的继承和普通用户读取授权。

M1-002 批次交付包为 **17 个源码/说明文件，加 1 个生成清单**；M1-004 新增 5 个数据/说明文件、数据检查脚本和测试文件。环境、缓存、安装的解释器和历史包不进入归档。最新包名、实际文件数、完整校验值、字节数和本地版本目录见 `artifacts/M1/latest-delivery.json`；每个 ZIP 旁另保存 `.sha256` 和 `.verification.json`。检查脚本、预览和本地解压目录位于已忽略的 `artifacts/M1/`，不进入源码包。文档更新后重新生成并核验最终包。

## 范围与后续

当前仅应用轻量锁文件存在。前端和训练目录加入时，规则强制携带对应 manifest/锁文件；本次未创建训练环境或伪造训练锁文件。打包器按文件范围和命名排除，不扫描源码内容中的凭据；资源新增需维护规则。

解释器安装使用 `--no-bin --no-registry`，位于项目产物目录；没有修改系统/用户环境变量或替换 Miniconda。测试使用系统临时目录，其中的临时 Git 仓库与 junction 均为测试夹具。

服务器未连接，远程更新、Linux 实跑、业务流程与 GPU 验证均未发生。M1 仍在进行，M2–M6 未开始。

## M1-004 最小业务规格与数据检查（2026-09-17）

`data/business/v1/` 包含 `README.md`、`spec.json`、`orders.json`、`operations.json` 和 `scenarios.json`，明确整数分、固定业务时间、模拟政策期限、确认要求及四类任务的手工期望。4 个订单、1 笔有确认快照的历史退款、5 个独立样例已经通过数据检查；完整政策文本和约 30 个边界案例仍待 M1-005/006。

实际运行 `uv run --no-sync --cache-dir .uv-cache python scripts/check_business_data.py`，返回 `fixture_check_passed`。证据 `artifacts/M1/business-data-check.json` 保留执行来源、命令、数据哈希和预期结果。该入口共用配置模块，只读数据，不创建数据库，输出明确标注 `business_execution: not_run`。

本轮 9 项测试覆盖四类样例及退款期限/26 小时延迟事实，拒绝浮点/布尔/负数金额、缺失初始账本、确认金额不符、重复退款期望和错误身份；跨目录 CLI 检查确认未改动数据或创建运行目录。新脚本只检查初始样例一致性，不能代替 M1-024 业务规则测试或 M3 恢复验收。没有安装新依赖、运行模型或连接服务器。

打包规则将 5 个数据/说明文件和检查脚本列为必需项。最终交付检查沿用 `.venv/Scripts/python.exe artifacts/M1/verify_delivery.py`，除既有哈希、排除规则、解压和 Windows 读取权限检查外，运行解压源码中的数据检查入口；详见 `artifacts/M1/latest-delivery.json` 的 `extracted_business_data` 结果。旧测试套件未重跑。
