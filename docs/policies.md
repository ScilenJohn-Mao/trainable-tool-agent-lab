# 政策读取与检索

`knowledge.ingest.PolicyCatalog.from_file(path)` 读取 UTF-8 `policies.json`，保留
全部版本、品类、有效期、来源规格与条款。导入检查文档 ID/版本唯一、条款 ID
唯一、文本非空及带时区的有效区间；不生成或改写业务数据、索引文件和数据库。

`catalog.read_policy(ReadPolicyArgs(...))` 按明确的 ID、版本和可选 section 读取。
指定 section 返回该条款原文；省略 section 或指定 `/` 返回完整文档，各条款前
保留 `[section_id]` 标记，引用位置为 `/`。`/` 是保留位置，不能作为数据中的条款 ID。
找不到文档、版本或条款时抛出 `KeyError`，不会替换为其他版本。
显式读取允许查看旧版/未来版；结果有效期仍须由调用方核对。

`knowledge.search.PolicySearch.from_data_dir(data_dir, business_time=None)` 导入政策，
默认从同目录 `spec.json` 读取固定业务时间；可由运行时传入带时区的仿真时间。
每个实例按该时间建立内存条款索引，适用区间是 `[effective_from, effective_to)`。
改变时间或数据后重新创建实例。系统日期不会影响检索资格，模型参数没有时间字段。

`engine.search_policy(SearchPolicyArgs(...))` 返回共享 `PolicySearchResult`，每个命中
含政策 ID、版本、有效期、section、标题、完整条款原文和 BM25 分数，可直接按引用读取。
`category=general_goods/digital_goods` 同时包含 `all` 通用说明；`category=all` 只返回
通用说明，未知品类无结果。`version` 进一步限制已生效的文档，不绕过有效期。
默认最多 5 条，可指定 1–10 条；没有词项匹配返回空 hits。

## 排名与切分

标题和条款共同参与 BM25。采用 `k1=1.2`、`b=0.75`，IDF 为
`ln(1 + (N-df+0.5)/(df+0.5))`，每个查询词项贡献为
`IDF * tf * (k1+1) / (tf + k1*(1-b+b*length/avg_length))`。
N、df 和平均长度基于实例中所有有效条款；品类/版本过滤不会重算统计。
公式参数参考 [Lucene BM25Similarity](https://lucene.apache.org/core/9_12_2/core/org/apache/lucene/search/similarities/BM25Similarity.html)。

文本先做 NFKC 归一化与小写化，中文按单字及相邻双字切分，英文/数字/下划线
标识符按连续片段切分。查询词项去重，无同义词扩展、语义推断或词级中文分词库。
仅返回正分结果，按分数降序，同分按 ID、版本、section 排序，结果不依赖文件顺序。
该词法基线尚未以独立标注检索集衡量 Recall；BM25 分数不是业务资格或置信度。

## 使用

从项目目录执行：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.knowledge.search search "延迟补偿券固定500分" --category general_goods --limit 3
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.knowledge.search read P-DELAY-AMOUNT mock-policy-v1 --section amount
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.knowledge.search read P-REFUND-ELIGIBILITY mock-policy-v0
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_policy_search.py
```

CLI 输出 UTF-8 JSON。`--data-dir` 可选其他数据目录，显式相对目录以当前工作目录为准；
不指定时使用应用配置的 `business_data_dir`。`--business-time` 接受带时区 ISO 时间，
放在 `search/read` 子命令前。读取只需要政策文件，搜索默认还需要规格中的固定时间。

Python 示例：

```python
from tool_agent_lab.knowledge.search import PolicySearch
from tool_agent_lab.settings import load_settings
from tool_agent_lab.tools.contracts import ReadPolicyArgs, SearchPolicyArgs

engine = PolicySearch.from_data_dir(load_settings().business_data_dir)
result = engine.search_policy(SearchPolicyArgs(query="延迟补偿券固定500分", limit=3))
for hit in result.hits:
    document = engine.catalog.read_policy(ReadPolicyArgs(
        policy_id=hit.reference.policy_id,
        version=hit.reference.version,
        section=hit.reference.section,
    ))
    assert document.text == hit.excerpt
    print(document.reference.model_dump_json(), document.text)
```

这些入口只提供政策证据，不启动 MCP 传输、调用模型或执行退款。
文档引用 `P-* / section` 与业务规则引用 `R-* / spec.json#/rules/...` 用途不同；
文档命中不能代替业务规则复核、当前提案或人工批准。
