# 本地 MCP stdio 工具

应用使用官方 MCP Python SDK 的 1.x 接口，精确安装版本由应用 uv.lock 固定。
服务端通过 stdin/stdout 交换 UTF-8 JSON-RPC；stdout 只用于协议消息，日志写入 stderr。
客户端启动真实子进程，执行 initialize、tools/list 和 tools/call，并在退出会话时关闭子进程。
传输和生命周期由 SDK 管理，参考 [官方 stdio 规格](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
与 [Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0)。

## 安装与启动

在项目轻量应用环境同步锁文件：

```powershell
uv sync --locked --cache-dir .uv-cache
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.tools.client list
```

第二条命令实际启动服务并输出协商的协议版本、服务信息及七工具目录。
SDK 包括 HTTP/认证等传递依赖，本地入口只用 stdio，不下载模型或启动网络监听。

供外部 MCP host 启动的服务命令为：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.tools.server
```

此命令等待 MCP 输入，不会打印交互提示。host 配置宜直接使用应用虚拟环境的 Python，
参数为 `-u -m tool_agent_lab.tools.server`，工作目录为项目目录。

## 调用与本地身份

四个只读工具调用实际服务：get_order/get_operation 读取应用 SQLite，
search_policy/read_policy 读取版本化政策。业务读取按启动时配置的本地应用用户过滤；
工具参数或 MCP 元数据不能切换身份。get_operation 保留 pending/succeeded/failed 或 null；
null 不代表业务失败，当前只读入口尚未绑定具体任务/尝试。

Standalone CLI sessions have no trusted write binding: request_refund, issue_coupon and
create_handoff return execution_context_required/not_committed. Runtime callers use
[ToolExecutor](executor.md) to publish proposals, obtain a human decision through
ApprovalService, and prepare a persisted operation before the protected MCP call.
Trusted get_operation sessions validate the complete attempt identity through BusinessService.

默认数据目录、业务库和用户来自应用配置。服务与客户端都支持
`--database`、`--data-dir`、`--owner-id` 启动选项；它们是本地操作者配置，
不属于模型工具参数。显式相对路径按当前工作目录解析。
缺少业务库时返回 database_not_initialized，不自动创建或载入订单；政策工具仍可用。

Windows PowerShell 推荐把 JSON 从 stdin 传入，避免原生命令参数转义差异。
下面的 OutputEncoding 只设置当前 PowerShell 会话的管道编码：

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
'{"query":"延迟补偿券固定500分","category":"general_goods","limit":1}' | uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.tools.client call search_policy - --call-id search-demo
'{"policy_id":"P-DELAY-AMOUNT","version":"mock-policy-v1","section":"amount"}' | uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.tools.client call read_policy -
'{"order_id":"ORD-1001"}' | uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.tools.client call get_order -
```

客户端 stdout 是带 call_id 的结构化 JSON；工具成功退出 0，结构化拒绝/失败退出 1，
命令行格式错误退出 2。订单未初始化或不属于该用户时会返回明确错误。
不确定结果不自动重试写调用。

## Python 接口与结果

```python
import asyncio

from tool_agent_lab.tools.client import open_tool_session

async def main():
    async with open_tool_session() as client:
        print(client.initialization.protocolVersion)
        reply = await client.call_tool(
            "search_policy", {"query": "延迟补偿券固定500分", "limit": 1},
            call_id="search-demo",
        )
        print(reply.result.model_dump_json())
        assert reply.raw.structuredContent == reply.result.model_dump(mode="json")

asyncio.run(main())
```

`local_server_parameters(database=..., data_dir=..., owner_id=..., cwd=...)` 可构造隔离会话。
`open_tool_session(parameters, errlog=...)` 返回 ToolClient；
`list_tools()` 返回实际服务目录；`call_tool(name, arguments, call_id=None)` 返回 ToolReply，
同时保留 SDK 原始 CallToolResult 和共享契约解析后的 result。
不提供 call_id 时客户端生成 UUID；它通过 MCP 元数据传递，不混入模型参数，
服务端没有该元数据时以 JSON-RPC 请求 ID 关联。
客户端核对 call_id 和 isError/status；缺结构化结果或对应关系不一致时直接报错。

服务端集中复用已有 Pydantic 参数契约和结果契约，协议工具目录导出同一份 JSON Schema。
无效参数、找不到订单/引用和缺可信写绑定均有结构化错误及匹配 call_id。
TextContent 和 structuredContent 保存同一份 JSON，便于原始输出归档。
应用初始化、真实授权与业务执行接口另见任务/确认和业务服务说明。

运行实际子进程测试：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/integration/test_mcp_stdio.py
```
