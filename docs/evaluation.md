# 固定场景评测

评测使用冻结场景、同一 Agent 图和真实 MCP 业务服务。每条轨迹创建独立订单库和 checkpoint，仿真用户沿原输入、提案确认与恢复入口交互。默认移除临时轨迹目录，JSON 报告保留每次尝试的完整业务快照、图状态和事件；`--keep-artifacts` 可保留原库。

本地工程检查：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m scripts.evaluate --split validation --limit 8 --scripted-demo --output artifacts/evaluations/scripted-check.json
```

`--scripted-demo` 明确使用了解场景标签的脚本回复，只检验工程路径，不代表自主模型成功率。mock 的 token 用量缺失，报告记录 `tokens: null` 和缺失调用数，不估算 token。

模型评测：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m scripts.evaluate --split validation --model-config path/to/prepared-model.yaml --output artifacts/evaluations/model-validation.json
```

将配置路径替换为实际已准备的模型 YAML。支持既有 local/HTTP provider，复用一个模型客户端，实际运行须准备配置所指的权重或服务。provider、模型名/版本、提示、预算、解码、冻结数据清单和 SHA-256 均写入报告；完整权重 revision 应在真实模型配置与实验记录中固定。凭据通过已有环境变量读取，配置导出排除 API key。

`--scenario ID` 可重复选择固定划分中的记录；`--limit N` 按冻结文件顺序截取。报告同时保存划分总数和实际选择 ID，子集结果只代表该子集。默认不重跑；可明确设置 `--infrastructure-retries 1`，最大 3 次额外尝试。只重跑基础设施异常，每次用新隔离库，保留所有失败与重跑成本。业务失败和无效证据不会自动重跑。

自定义 mock 使用 `--mock-responses file.json`，文件为场景 ID 到 assistant 消息列表的映射，消息遵守既有模型回复契约。缺失或用完脚本归为无效轨迹，报告保留覆盖缺口。输出必须是新路径，已有报告不会覆盖；运行中逐次保存，未执行场景仍保留在计划分母。

判分是确定性的结构化业务核验，不调用 LLM judge，也不以模型声称成功作为证据。成功要求终局完成、目标金额/动作/订单/转人工原因正确、真实账本与余额一致、确认归属/参数/消费时间有效、需要的澄清及原操作键查询完成、结构化结论事实与账本及规则/政策引用相符。最终面向用户的结构化结果由已有结果构建器生成；自由文本不作为额外语义评分对象。

正常待确认提案或用户拒绝不算越权；违反规则的动作尝试、工具错误、未完成等待及错误金额均为业务失败（reward 0）。成功 reward 1。传输、子进程或 SQLite 操作异常归为 infrastructure；缺失/损坏证据及脚本配置问题归为 invalid，二者 reward 为 null，明确列出缺口。非脚本模型格式失败与决策预算耗尽为业务/模型失败；未分类异常保留为无效证据。

汇总同时输出 `successful/planned`、`successful/scored`、覆盖率、四类结果、失败标签、重跑次数、所有尝试和未成功尝试成本。只有所有调用都返回完整用量时才给总 token；部分可知用量放入 `known_tokens`。工具调用数包括模型提出但被拒绝的调用。耗时使用实际单调时钟：model 为 generate，tools 为提案/执行节点（包含 MCP 启动），waiting 为自动用户构造回复之前的等待，orchestration 为其余初始化、图调度、快照和清理。业务固定时间不用于计算耗时；首次模型加载在首次 generate 内。

根场景配对按 `root_scenario_id` 与 `variant` 计算，只有一条 normal 和对应唯一故障变体才形成配对。当前冻结集仅含正常业务场景，报告 `pairing.available: false`；改写及不同业务实例不会被当作故障配对。故障集就绪后可直接使用相同汇总函数，不能用脚本异常检查替代正式故障评测。

CLI 打印报告路径和摘要；全部选定场景成功返回 0，有失败或覆盖缺口返回 1。只生成 JSON，不启动报告 API 或页面服务。
