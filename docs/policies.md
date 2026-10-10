# 政策读取与检索

`knowledge.ingest.PolicyCatalog.from_file(path)` 读取 UTF-8 `policies.json`，保留
全部版本、品类、有效期、来源规格与条款。导入检查文档 ID/版本唯一、条款 ID
唯一、文本非空及带时区的有效区间；不生成或改写业务数据、索引文件和数据库。

`catalog.read_policy(ReadPolicyArgs(...))` 按明确的 ID、版本和可选 section 读取。
指定 section 返回该条款原文；省略 section 或指定 `/` 返回完整文档，各条款前
保留 `[section_id]` 标记，引用位置为 `/`。`/` 是保留位置，不能作为数据中的条款 ID。
找不到文档、版本或条款时抛出 `KeyError`，不会替换为其他版本。
显式读取允许查看旧版/未来版；结果有效期仍须由调用方核对。

## 引用定位与校验

`knowledge.citations.validate_citation(catalog, reference, ...)` 接受共享
`PolicyReference`，按 policy_id/version/section 回读原始 `PolicyDocument`。
版本和条款必须明确存在，有效期必须与该版本一致；没有版本回退，也不会把
条款 ID 当作文本偏移。`section="/"` 定位带条款标记的完整文档。
可选 `excerpt` 必须等于定位条款的完整原文（或 `/` 的标记全文），不自动修剪、
归一化或接受模型改写。返回的引用、标题、品类和文本均来自政策目录。

默认允许定位历史/未来政策。提供带时区的 `business_time` 时，另检查
`[effective_from, effective_to)`；提供 `category` 时，检查品类与检索相同的
规则：具体品类包含通用 all，all 仅通用，未知品类拒绝。
`CitationError.code` 为 `citation_not_found`、`citation_interval_mismatch`、
`citation_excerpt_mismatch`、`citation_not_active` 或 `citation_category_mismatch`。
不带时区的业务时间抛出 `ValueError`。

Agent 收集搜索/读取证据时核对原文与引用，生成结构化结果时再核对保存的引用。
显式历史读取在结果中保留原版本与有效期，不能据此认为政策当前适用。
这些检查确认出处和适用范围，不判断文字是否支持某个结论，也不授予退款权限。
业务规则引用 `R-*` 不属于政策文档目录，仍由业务服务单独校验。

## 检索

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
先按业务时间、品类和指定版本选出候选条款，再基于候选计算 N、df 和平均长度。
不适用品类、其他版本和失效条款不参与评分统计。
公式参数参考 [Lucene BM25Similarity](https://lucene.apache.org/core/9_12_2/core/org/apache/lucene/search/similarities/BM25Similarity.html)。

文本先做 NFKC 归一化与小写化，中文用内置售后词表按最长词优先切分，例如
“退款金额”切为“退款/金额”，“不确定转人工”切为“不确定/转人工”。查询与文档
采用相同切分；“请问/我的/是否/可以”等常见表达不参与评分。未知连续片段按相邻
双字切分，孤立单字保留；不再为每个已识别词额外加入单字，避免“退市”仅凭“退”
命中退款政策。“不退款”保留“不/退款”，不会将否定改写为肯定。
英文/数字/下划线标识符按连续片段切分，全角金额归一化为半角。
查询词项去重，无第三方分词依赖、同义词扩展或语义推断；词表面向当前售后业务，
未知词的双字匹配仍可能召回不相关条款。
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
from tool_agent_lab.knowledge.citations import validate_citation
from tool_agent_lab.settings import load_settings
from tool_agent_lab.tools.contracts import SearchPolicyArgs

engine = PolicySearch.from_data_dir(load_settings().business_data_dir)
result = engine.search_policy(SearchPolicyArgs(
    query="延迟补偿券固定500分", category="general_goods", limit=3,
))
for hit in result.hits:
    document = validate_citation(
        engine.catalog, hit.reference, excerpt=hit.excerpt,
        business_time=engine.business_time, category="general_goods",
    )
    assert document.text == hit.excerpt
    print(document.reference.model_dump_json(), document.text)
```

这些入口只提供政策证据，不启动 MCP 传输、调用模型或执行退款。
文档引用 `P-* / section` 与业务规则引用 `R-* / spec.json#/rules/...` 用途不同；
文档命中不能代替业务规则复核、当前提案或人工批准。
