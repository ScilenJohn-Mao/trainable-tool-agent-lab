# ART 训练接入

从项目根目录调用入口。配置检查在应用轻量环境中执行，不导入 ART、torch 或模型：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m scripts.train_art --check
uv run --no-sync --cache-dir .uv-cache python -m scripts.train_art --config configs/training/pilot.yaml --check
```

检查会输出冻结数据、实际锁 SHA-256、模型与预算配置，并列出服务器资产和实际验证需求。`base_revision: null` 表示没有虚构基座 revision；训练前必须准备本地 Qwen2.5-7B-Instruct 权重并提供已核验的40位 revision。`--check` 成功只表示本地配置与冻结资源有效。

Linux 使用两个独立环境：

实际训练前暂停同卡应用推理。`gpu_memory_utilization` 只是 vLLM 的配置，不代表全部进程的总显存上限，实际资源仍需测量。

```bash
uv sync --project training/art --locked --cache-dir .uv-cache
uv sync --project training/art/runtime --locked --cache-dir .uv-cache
training/art/runtime/.venv/bin/python training/art/runtime/prepare.py --apply
training/art/runtime/.venv/bin/python training/art/runtime/prepare.py --check
uv run --project training/art --locked --no-sync --cache-dir .uv-cache python -m art_trainlab.train --config configs/training/smoke.yaml --base-revision VERIFIED_REVISION --train
```

将 `VERIFIED_REVISION` 换成实际已验证的基座 revision。训练入口只使用本地资产，关闭进程内 HF 下载、W&B、Weave 和 LangSmith 外部跟踪，设置 ART runtime 项目路径。准备环境和缓存后，仍需实际断外网验证采样与更新。源码不包含权重或凭据；环境准备与训练会使用大量磁盘/显存。

`runtime/prepare.py` 不导入 torch 或 vLLM。它核对实际 runtime/vLLM 版本、Git revision和策略标注源码 SHA-256，再原子应用一个固定兼容补丁：vLLM 的 `dummy_run` 预热/图捕获仍执行模型，但跳过 ART 对虚拟请求的策略标注并清理残留策略上下文；真实请求仍由原 ART 路径严格核对。未知源码或已有额外修改明确拒绝，不按请求 ID 前缀忽略错误。重复执行不重复修改文件；依赖重新同步后应再次执行。训练 preflight 要求补丁已安装，并在训练报告中记录原始/补丁后的源码 hash，不能只凭未变的包版本认定源码一致。执行准备命令前停止受影响的 ART/runtime 进程。此 CPU 检查不代表实际 CUDA 预热或训练更新已通过。

ART 固定为0.5.20、Git revision `12162f241320f2343f1df162e834dc5ae03f740d`，runtime也来自同一revision的`vllm_runtime`子目录，固定vLLM0.25.1+cu129。复用其`init_chat_model`、`wrap_rollout`、`Trajectory/LegacyHistory`和`LocalBackend.train`；共享LangGraph、七工具、原确认/事务服务和业务验证器均沿原路径。参考[ART LangGraph接口](https://art.openpipe.ai/integrations/langgraph-integration/)。

这套固定runtime要求GPU计算能力至少7.5，V100的7.0不在标准支持范围。入口会在加载前拒绝不符合条件的GPU，不能把7B直接推理通过视作ART兼容；实际运行需选择符合该栈的GPU，或另行验证并重新固定兼容栈。[固定版本的vLLM要求](https://raw.githubusercontent.com/vllm-project/vllm/v0.25.1/docs/getting_started/installation/gpu.cuda.inc.md)。

每组同一场景/用户规则/策略版本，G=2、活跃生成1、每次更新1组；不是两个rollout并发。smoke目标2次真实更新，最多采样10组；pilot目标10次更新，最多50组。完整或部分失效组不跨场景补齐，不允许无logprob更新；零方差组保留记录但不提交更新。采样预算用尽后更新不足返回1并报告incomplete_updates，不将采样次数称作梯度步数。

单次输出2048、最多12次决策，预算传入真实completion请求和共享图；vLLM max_model_len及ART packed_sequence_length为8192。记录实际prompt/completion token IDs和对应logprob，assistant mask只选当前采样输出，历史assistant、用户、政策与工具结果位于prompt部分。跨等待/resume仍属于同一rollout，同一completion重复记录只保留一次，不允许不同内容复用ID。完整轨迹的组装后总训练token也限制8192，超长明确丢弃，绝不静默截断。

ART包装的自动会话转换有静默跳过分支，因此额外保留原始completion与实际发送的消息/工具schema。转换结束后以原始choices补齐每次请求的LegacyHistory，保留工具调用原参数及token元数据；训练前再调用ART官方序列组装器逐项比对token、mask和logprob。上下文中的重复assistant文本不会重复计入训练mask。没有真实token/logprob时不会根据文字估算，也不会制造可训练轨迹。

产物位于`artifacts/training/<run>/`：逐rollout的`completions.jsonl`、业务/轨迹/丢弃记录及`training.json`，保留基础设施和无效轨迹、业务0/1奖励、实际组大小、零方差、更新与checkpoint路径。ART backend数据位于`artifacts/training/backend/`；包装日志`.art/`被Git及源码包排除。基础设施/无效轨迹不会静默变成0奖励。`--train`才调用真实backend；本地替身检查只验证接线、记录和业务路径，不能证明GPU参数更新、保存重载或adapter回接。
