# 固定场景数据

`data/scenarios/v1/` 包含 train 400、validation 100、test 200。
四类任务在每个划分中均衡；train有320个业务实例及80条同划分改写，validation有80个实例及20条改写。
test的200条均是独立新实例，其中100条使用开发集已知条件，50条保留未见条件组合，50条使用未见v2政策。
重复改写不会被算成新的业务实例，正常与故障配对另行处理。

每条记录含独立订单初始状态、历史批准退款描述、场景输入、仿真用户回复和期望。
`instance_id` 与 `family_id` 绑定同一业务实例，改写留在同一划分；文本模板家族在各划分间独立。
条件指纹由品类、核实状态、期限/延迟区间、已有退款、用户交互及请求目标计算，忽略金额和订单号等实例值。
未见组合与train/validation条件指纹不相交。v2的完整规则与政策只供其test场景使用，开发政策资产不含v2文档。

两套业务资产分别固定于2026-09-17和2026-10-17的12:00 +08:00。
v1退款期限7天、延迟补偿500分；v2期限10天、补偿800分，复用版本化业务规格。
初始已退款状态通过原提案、批准和业务事务重建，不能直接写入退款结果或跳过授权。

## 构建与核验

在项目根目录执行：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m scripts.build_dataset --profile small --output artifacts/datasets/small
uv run --no-sync --cache-dir .uv-cache python -m scripts.build_dataset --output data/scenarios/v1
uv run --no-sync --cache-dir .uv-cache python -m scripts.build_dataset --output data/scenarios/v1 --verify
```

构建拒绝覆盖任何已存在的目标目录，复现实验使用新目录。固定seed和相同源码产生相同字节。
manifest冻结数据文件hash/大小、划分统计、规则/检索实现及原始业务资料指纹、两版本实际BM25条目指纹。
冻结数据通过`.gitattributes`保留精确字节，源码/原始文本指纹统一换行为LF，避免Windows/Linux检出差异。
核验检查文件、标签可解性、金额、划分隔离及指纹；修改数据、规则或索引时应建立新数据版本。
`review_sample.jsonl` 是按模式和样本组抽出的50个不同实例，作为人工检查输入；文件存在不代表已被人类审查。
期望来自声明的场景规格，BusinessRules仅用于检查标签，不用其运行结果自动生成标签。

## 接入共享环境

```python
from tool_agent_lab.evaluation.datasets import load_dataset
from tool_agent_lab.schemas.scenarios import ExperimentConfig
from tool_agent_lab.simulation.environment import open_environment

async def run_one(model, agent_config):
    scenario = load_dataset("data/scenarios/v1", "train")[0]
    experiment = ExperimentConfig(model=model.config, agent=agent_config)
    async with open_environment(scenario, experiment=experiment, model=model) as environment:
        return await environment.run()
```

加载器先核验冻结清单，再将相对资产路径绑定到数据目录。模型只接收用户输入及原图提供的事实/工具结果，
不接收划分标签、期望或未来用户决定。真实模型评测、独立终局判分、业务奖励和ART训练使用各自入口，
数据检查与脚本模型工具运行不能代替这些结果。
