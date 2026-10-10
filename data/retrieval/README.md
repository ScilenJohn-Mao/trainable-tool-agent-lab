# 政策检索查询集

`queries.jsonl` 保存 50 条 UTF-8 JSON 查询，面向
[`data/business/v1/policies.json`](../business/v1/policies.json) 的固定政策语料。
包括 40 条当前版本问题、4 条在各自有效时间查询的旧版/未来版问题，以及
6 条没有相关依据的查询。覆盖 20 个政策主题、24 份版本化文档，包括口语改写、
整数分与全角金额、相似业务条件、组合依据、品类/版本过滤及有效期边界。

每行字段：

- `query_id`：稳定查询 ID。
- `query`：传给检索接口的自然语言问题。
- `category`、`version`：检索过滤条件；null 表示不限定该条件。
- `business_time`：带时区的固定业务时间，不能用系统日期替代。
- `tags`：业务主题和案例特征，可用于分组检查。
- `expected.relevant_sections`：手工标注的相关条款，每项保存
  policy_id/version/section 三元组，以及原文中的 `evidence` 依据片段。
- `expected.rationale`：为什么这些条款回答问题，或为什么没有适用依据。

标注依据政策原文及其来源规格写定，未从检索输出、排序或分数生成。
每个条款版本和位置必须明确，依据片段必须能在该条款原文中找到；片段用于
核对标注，不是 `validate_citation(excerpt=...)` 所要求的完整条款原文。
时间与品类必须与引用一致。历史/未来时钟只选择检索语料，不切换当前业务服务。

相关条款按 section 粒度标注，不规定返回顺序，也不固定唯一工具流程。
RQ-016 同时标注组合兼容性及退款、补偿资格三个依据。标注是这份小型语料中的
目标依据集合，其他未标条款可能提供补充背景，不应一律当作错误答案。
空集合表示过滤后没有候选或问题没有相关政策，原因由 rationale 和 tags 区分。

后续计算 Recall@k 时，按每条有依据查询的已命中三元组数 / 标注三元组数计算，
另行报告正例总数、宏平均及各标签/版本结果；无依据查询单独报告空结果比例，
不把它们记作 Recall=1。固定 k、查询文件、政策语料和检索实现后再比较结果。
这份查询集用于检索质量核对与诊断，不等同于独立业务 test 集或模型泛化成绩。

只读核对标注格式、引用、依据与适用范围：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q -p no:cacheprovider tests/unit/test_retrieval_queries.py
```

输入检索时只传 query/category/version，并用 business_time 构造检索实例。
`expected`、rationale 和 tags 留在评测侧，不传给模型或加入政策索引。
查询和标注不能代替人工确认、业务规则或真实训练轨迹。

运行检索评测并保存可查看的 UTF-8 JSON 报告：

```powershell
uv run --no-sync --cache-dir .uv-cache python scripts/evaluate_retrieval.py --output artifacts/runtime/retrieval-baseline/report.json
```

默认计算 k=1/3/5/10，可用 `--k 1 5` 选择 1–10 中的值。
`--queries`、`--policies` 可指定其他文件；显式路径相对于当前工作目录，
不传 `--output` 时将完整报告输出到终端。无需启动 API、worker 或加载模型。

`summary` 给出正例宏平均 Recall、按条款总数计算的微平均 Recall、完整命中查询数，
以及单独的无依据查询空结果比例。`by_expected_version` 和 `by_tag` 保留分组结果；
标签可以重叠，各组不能相加当作样本总数。每条 `queries` 保存排名、分数、完整条款原文、
各 k 的漏检条款与引用错误。命中须精确匹配 policy_id/version/section，版本正确但条款不相关
不能算作命中。`returned_versions` 统计最大 k 的返回版本，`version_filter_violations`
列出违反显式版本过滤的返回项。引用检查复用原文、有效时间和品类校验，
只表示出处及适用性正确，不保证语义支持或业务授权；失败项仍保留在报告中。

`inputs` 保存查询、政策、检索、引用、语料加载及评测代码的 SHA-256，
便于比较时核对输入与实现。报告是这份小型已标注语料的检索基线，
不能代替真实模型或业务流程验收，也不计算缺少完整相关性标注的 Precision。
