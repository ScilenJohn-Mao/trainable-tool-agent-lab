# 七工具契约

`tool_agent_lab.tools.contracts` 只依赖 Pydantic 和共享 schema，提供参数、结构化结果、
错误及 JSON Schema 定义，不创建数据库、执行工具或建立传输会话。

| 工具 | 模型参数 | 成功结果 data | 确认 |
|---|---|---|---|
| get_order | order_id | Order | 不需要 |
| search_policy | query，可选 category/version，limit 默认 5、范围 1–10 | PolicySearchResult：query、hits | 不需要 |
| read_policy | policy_id、version，可选 section | PolicyDocument：reference、title、category、text | 不需要 |
| request_refund | RefundAction | RefundOperation：已成功退款账本 | 需要 |
| issue_coupon | CouponAction | CouponOperation：已成功补偿账本 | 需要 |
| get_operation | operation_key | OperationLookup：原键及 Operation/null | 不需要 |
| create_handoff | HandoffAction | HandoffOperation：已成功转人工账本 | 需要 |

写参数直接复用既有动作 schema，默认 action 与工具名称一致。金额为严格正整数
CNY 分，转人工参数没有金额。模型参数不接受 owner、task/attempt、ExecutionContext、
确认记录、approved、写 operation_key 或 call_id；读取操作键是 get_operation 的参数。
可信运行时负责身份、业务时间、提案/批准/操作绑定与调用 ID。解析成功不授予权限。

`TOOL_CONTRACTS[name]` 提供 `parse_arguments(dict)`、
`parse_result(dict, call_id=expected_id)` 和 `definition()`。
`tool_definitions()` 返回固定七工具的列表，含 name、description、inputSchema、
outputSchema 和只读/封闭业务范围提示。提示仅为元数据，不能代替实际授权检查。
定义字段及 structuredContent 约定依据 [MCP 工具规格](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。

结果统一携带非空 call_id，parse_result 会核对预期 ID：

- `status="ok"`，data 为工具对应的结构化对象。写工具只允许同类 succeeded 账本，
  必须包含提交时间；pending/failed 不能放在写成功分支。结构本身不证明账本存在，
  服务/执行器必须从实际存储结果构造它。
- `status="error"`，error 包含 code、message 及明确 outcome。
  已知未提交使用 `outcome="not_committed"`，可附原 operation_key。
  不确定结果使用 `outcome="unknown"`，必须提供调用前保存的 operation_key。
  不确定不能作为 failed 或“未发生业务”处理，须先按原键核对。

两分支不能混用。get_operation 的 ok 只表示查询返回，operation 保留
pending/succeeded/failed，或为 null；其他归属与未知键不会暴露账本。
OperationLookup 检查返回账本和查询键一致。政策命中与正文保留完整版本化
PolicyReference（ID、版本、生效区间和引用位置）；空搜索 hits 是正常结果。
这些 schema 不保证政策实际已检索、资格成立或引用可定位，仍需服务检查。

```python
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS

lookup = TOOL_CONTRACTS["get_operation"]
arguments = lookup.parse_arguments({"operation_key": "original-refund-key"})
result = lookup.parse_result({
    "call_id": "call-1",
    "status": "ok",
    "data": {"operation_key": arguments.operation_key, "operation": None},
}, call_id="call-1")
assert result.data.operation is None
```

上述只展示查无记录的结构，不执行查询。服务器接入时应把校验后的对象作为
structuredContent，并在文本 content 中提供 JSON，错误分支设置 isError=true。
本模块不输出 MCP CallToolResult、启动 stdio 或模拟已有工具服务。

从任意工作目录导出 JSON Schema 目录（标准输出 UTF-8）：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.tools.contracts
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_tool_contracts.py
```

第一条命令仅输出 `{"tools": [...]}`，不写文件。参数/结果都拒绝未知字段，
JSON Schema 描述结构；金额、时间区间与跨字段关系同时由共享 Pydantic 校验执行。
