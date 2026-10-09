# 源码包与服务器更新

源码包包含项目代码、依赖锁文件和运行资源。Windows/Linux 共用应用代码；这里的安装命令只同步轻量应用依赖，用于 mock/HTTP/MCP/模拟业务。直接模型推理采用独立 Transformers + PEFT 环境；当前版本尚无直接加载器及相应安装/启动命令，不应把下面的轻量同步当作模型环境安装。

模型资产独立保存在项目根目录 `models/base/Qwen2.5-3B-Instruct/`、`models/adapters/<version>/`，完整基座与训练后 adapter 的要求见 [README](../README.md#本地模型文件)。根目录 models 不在源码包白名单内，configs/models 属于必须交付的应用配置。部署新源码目录时保留并复制/挂载已有模型资产，或者配置到源码外的持久模型位置；不要期待源码 ZIP 内含权重、HF 配置或 tokenizer。

应用直接推理在每台机器内进行，不要求远程模型服务或服务器 IP；浏览器与 API 使用同机 HTTP/SSE。Linux 的 RL 环境依赖固定版本 ART，专用 vLLM runtime 按该版本 vllm_runtime 的 pyproject/uv.lock/setup.sh 单独建立 uv 环境，通过 ART 支持的 runtime executable 绑定，不在训练主环境混装裸 vLLM。依赖、模型与编译资源先准备齐全，训练允许同机通信，禁用外部日志/存储/judge 后验证离线运行。adapter 导出为标准 PEFT 格式并附匹配基座/tokenizer/模板信息，单独搬回 Windows 加载，本机推理无需 ART/vLLM。

## 本地打包

在项目根目录用 Python 3.12 运行：

```powershell
.\.venv\Scripts\python.exe -I -S scripts/package_project.py --dry-run
.\.venv\Scripts\python.exe -I -S scripts/package_project.py
```

这里复用已有轻量环境的解释器；打包只用标准库，也可指定其他 Python 3.12 的路径，不要求安装项目、uv 或 Git。

检查预览，记录生成命令输出的包名、SHA-256、文件数、源码字节数和 ZIP 字节数。上传 ZIP 及同名 `.sha256` 文件。`PACKAGE_MANIFEST.json` 是生成文件，不纳入下一次源码选择；每次归档生成新的清单。

预览应包含代码、锁文件、SQL、业务数据/政策/案例、测试和运行说明，排除环境、缓存、真实配置、数据库、日志、权重及历史包。清单记录实际文件哈希和可用的 Git commit/dirty 状态，未提交文件也可能进入包，交付前核对它们。

用实际输出的完整路径核对整包哈希和权限：

```powershell
$package = '实际输出的完整 ZIP 路径'
Get-FileHash -LiteralPath $package -Algorithm SHA256
Get-Content -LiteralPath "$package.sha256"
Get-Acl -LiteralPath $package
Get-Acl -LiteralPath "$package.sha256"
```

ZIP 与校验文件继承输出目录的访问权限。Windows 验证同时核对普通用户的读取授权；只在创建文件的账号下通过解压，不能证明桌面账号可以打开文件。脚本采用普通唯一暂存目录，完成后只清理本次暂存文件和目录，不修改系统权限或要求管理员运行。

`package_rules.toml` 的 `include` 明确限定项目源码与资源。可选子项目目录存在时，相应依赖清单和真实锁文件成为必需文件；不存在时无需占位文件。

## Linux 服务器解压

以下是服务器操作示例，须在目标 Linux 主机实际运行并保存输出；本地 Windows 验证不能替代远程启动。服务器需已有 Python 3.12、uv 和 sha256sum。在上传目录设置实际文件名和一个尚不存在的版本目录，然后执行：

```bash
set -euo pipefail
package_name='trainable-tool-agent-lab-实际时间-实际内容版本.zip'
release_dir="$PWD/releases/实际版本"
sha256sum -c "$package_name.sha256"
mkdir -p "$(dirname "$release_dir")"
mkdir "$release_dir"
python3.12 -m zipfile -e "$package_name" "$release_dir"
```

命令串在校验失败或版本目录已存在时停止，不覆盖旧目录。进入解压后的唯一顶层目录 `trainable-tool-agent-lab/`，完成当前版本的轻量检查：

```bash
cd "$release_dir/trainable-tool-agent-lab"
uv sync --locked --python python3.12 --cache-dir .uv-cache
uv run --no-sync --cache-dir .uv-cache python -c 'import sys, tool_agent_lab; print(sys.version); print(sys.executable); print(tool_agent_lab.__file__)'
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.settings
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_boundary_cases.py -p no:cacheprovider
uv run --no-sync --cache-dir .uv-cache python scripts/package_project.py --dry-run
```

核对导入位置指向本次解压的源码。这些检查使用轻量应用环境，无需安装 GPU 依赖。

此同步仅针对当前轻量应用 pyproject.toml/uv.lock，不使用 all-extras/all-groups。Windows 的模型依赖由独立推理环境管理；ART 与 vLLM runtime 仅 Linux 训练安装。

## 隔离业务演示

在新版本项目目录运行，由操作者明确选择本次决定：

```bash
uv run --no-sync --cache-dir .uv-cache python scripts/demo_business.py --decision approved
uv run --no-sync --cache-dir .uv-cache python scripts/demo_business.py --decision rejected
```

批准应输出 refunded、12900 分退款、completed 任务、原操作键查询和重复调用均一致；拒绝应输出 rejected、operation=null、未变订单。每次使用新的 artifacts/demo/refund-唯一标识/app.sqlite3，不改配置中的应用库。提案在 stderr，结果 JSON 在 stdout；这是手工工具流程，金额单位、固定业务时间和确认约束见[退款说明](../docs/business_demo.md)。

## 持久数据与 HTTP 启动

业务库和日志放在 release 外的持久目录，通过启动进程环境提供配置。下面路径仅为示例，改为实际可写目录：

```bash
export TTAL_RUNTIME_DIR=/srv/tool-agent-lab/state
export TTAL_DEV_OWNER_ID=demo-user
export TTAL_MODE=manual
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.settings --init-dirs
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.storage.database
uv run --no-sync --cache-dir .uv-cache python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

业务库为 state/app.sqlite3，重复初始化保留数据，不自动载入订单。checkpoint 路径独立保留，当前 HTTP 入口不创建 checkpoint。不要将演示快照写入已有运行库；需要演示订单时先在新路径 seed，再将父目录指定为 HTTP 的 TTAL_RUNTIME_DIR，顺序见[README](../README.md#http-任务与确认服务)。服务器持久目录使用绝对路径，TTAL_* 相对路径锚定源码项目根目录。

源码包不含真实 .env。优先通过启动进程环境注入已有配置；若使用项目 .env，由操作者在新版本根目录配置。配置、运行数据、权重和可复用缓存独立保管，不依赖覆盖源码目录保留它们。

服务运行后，在第二个 Linux 终端冒烟：

```bash
curl --fail --silent http://127.0.0.1:8000/openapi.json
curl --fail --silent -H 'Content-Type: application/json' -d '{"user_message":"Please review my request"}' http://127.0.0.1:8000/tasks
curl --fail --silent 'http://127.0.0.1:8000/tasks?status=queued&limit=10'
```

OpenAPI 返回文档，创建返回 queued 任务，列表可查同一 task_id。空应用库创建请求不带 order_id；`/docs` 提供交互文档。当前 HTTP 不自动产生模型提案或执行退款，确认仅保存决定，受保护业务写入由执行器完成。详细字段及重试语义见[HTTP 接口](../docs/http_api.md)。

## 更新与切回

1. 停止旧 HTTP 进程或排空受影响任务，保留持久状态及备份，记录代码/配置路径。
2. 校验新包，在全新 release 中同步应用环境、核对导入及演示；存在前端目录时才按对应锁文件构建。
3. 用同一外部 TTAL_RUNTIME_DIR 和原配置启动新 release，核对 OpenAPI、任务与已有业务记录后切换实际服务入口。
4. 需要切回时停止新进程，进入保留的旧 release，用其环境、同一持久目录及原配置重新启动并冒烟。代码切回不撤销已提交退款；数据库迁移与数据恢复单独判断。

记录包名/校验值、release 路径、Python/导入路径、配置/持久目录、冒烟输出及实际切换情况。解压成功不等于应用启动成功。

更新运行中的服务时，先排空或停止受影响任务，再检查新版本、同步必要依赖及构建前端，最后切换入口。服务端 `.env`、业务数据库、checkpoint、权重与可复用环境放在独立持久目录，通过配置引用；不在训练过程中覆盖源码。使用新版本目录才能避免旧版已删除/改名文件继续生效。切回代码版本不会撤销已经发生的业务写入。
