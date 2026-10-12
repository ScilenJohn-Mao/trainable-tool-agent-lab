# 独立业务仿真

每次 `open_environment` 创建新的业务库、SQLite checkpoint、任务线程和日志目录。
Agent 使用原有图、工具执行器和真实 MCP 子进程，写操作继续经过提案、批准和业务事务。
MCP 会话沿现有执行器按调用打开并关闭，不在轨迹间共享可信上下文。

仿真用户只提交补充信息及具体提案的批准/拒绝。业务时钟由版本化规格固定，
用户脚本耗尽时保留等待状态，输出 `user_script_exhausted`，不会自动批准或伪造终局。
期望结果和用户脚本不注入模型上下文；场景的初始状态来自其绑定的业务 fixture。

## 脚本模型演示

在项目根目录执行：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m scripts.demo_simulation --scenario all
uv run --no-sync --cache-dir .uv-cache python -m scripts.demo_simulation --scenario refund-damaged --keep-artifacts
```

演示固定使用明确编排的 mock 回复和真实 MCP，不受应用模型环境变量覆盖，
展示运行及隔离能力，不代表模型效果或训练结果。
场景覆盖规格中的规则退款、退款加补偿、已有操作核对、澄清/转人工，另提供拒绝、
信息不足和只读查询变体。这些小样例不属于已冻结的训练或测试数据集。

默认回收本次轨迹目录；`--keep-artifacts` 保留业务库、checkpoint 和
`logs/trajectory.json`，默认父目录为应用 runtime 下的 `simulations/`。
`--output-dir` 可指定父目录，已有文件不会被覆盖或清理。
JSON 包含完整场景、实验配置、图状态、真实账本、订单和本工单事件，未计算成功奖励。
异常继续向调用方传播；异常退出时保存类型和消息，关闭数据库和工具资源。

## Python 接口

```python
from tool_agent_lab.schemas.scenarios import ExperimentConfig
from tool_agent_lab.simulation.environment import open_environment
from tool_agent_lab.simulation.scenario_generator import generate_scenarios
from tool_agent_lab.settings import load_settings

async def simulate(model, agent_config):
    settings = load_settings()
    scenario = generate_scenarios(settings.business_data_dir)[0]
    experiment = ExperimentConfig(model=model.config, agent=agent_config)
    async with open_environment(scenario, experiment=experiment, model=model) as env:
        return await env.run()
```

模型配置和 Agent 配置复用既有契约及版本字段；可注入遵循共享 generate 接口的模型客户端。
每个环境需独立的有状态 mock 客户端或独立回复序列，真实模型资产可由调用方管理。
再次打开同一场景即可重置，不重用此前的业务库和 checkpoint。
需要逐等待点检查时调用 `await env.start()`、`await env.respond()`；完整运行用 `await env.run()`。
