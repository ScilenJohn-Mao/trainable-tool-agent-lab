# 源码包与服务器更新

源码包包含项目代码、依赖锁文件和运行资源。按以下步骤生成、校验并解压到独立版本目录。

## 本地打包

在项目根目录用 Python 3.12 运行：

```text
python scripts/package_project.py --dry-run
python scripts/package_project.py
```

检查预览，记录生成命令输出的包名、SHA-256、文件数、源码字节数和 ZIP 字节数。上传 ZIP 及同名 `.sha256` 文件。`PACKAGE_MANIFEST.json` 是生成文件，不纳入下一次源码选择；每次归档生成新的清单。

ZIP 与校验文件继承输出目录的访问权限。Windows 验证同时核对普通用户的读取授权；只在创建文件的账号下通过解压，不能证明桌面账号可以打开文件。脚本采用普通唯一暂存目录，完成后只清理本次暂存文件和目录，不修改系统权限或要求管理员运行。

`package_rules.toml` 的 `include` 明确限定项目源码与资源。可选子项目目录存在时，相应依赖清单和真实锁文件成为必需文件；不存在时无需占位文件。

## Linux 服务器解压

在上传目录设置实际文件名和一个尚不存在的版本目录，然后执行：

```bash
package_name='trainable-tool-agent-lab-实际时间-实际内容版本.zip'
release_dir="$PWD/releases/实际版本"
sha256sum -c "$package_name.sha256" && mkdir -p "$(dirname "$release_dir")" && mkdir "$release_dir" && python3.12 -m zipfile -e "$package_name" "$release_dir"
```

命令串在校验失败或版本目录已存在时停止，不覆盖旧目录。进入解压后的唯一顶层目录 `trainable-tool-agent-lab/`，完成当前版本的轻量检查：

```bash
cd "$release_dir/trainable-tool-agent-lab"
uv sync --locked --python python3.12 --cache-dir .uv-cache
uv run --no-sync --cache-dir .uv-cache python -c 'import tool_agent_lab; print(tool_agent_lab.__file__)'
uv run --no-sync --cache-dir .uv-cache python -m pytest
uv run --no-sync --cache-dir .uv-cache python scripts/package_project.py --dry-run
```

核对导入位置指向本次解压的源码。这些检查使用轻量应用环境，无需安装 GPU 依赖。

更新运行中的服务时，先排空或停止受影响任务，再检查新版本、同步必要依赖及构建前端，最后切换入口。服务端 `.env`、业务数据库、checkpoint、权重与可复用环境放在独立持久目录，通过配置引用；不在训练过程中覆盖源码。使用新版本目录才能避免旧版已删除/改名文件继续生效。切回代码版本不会撤销已经发生的业务写入。
