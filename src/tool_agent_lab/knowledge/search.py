"""Section-level BM25 retrieval and a read-only policy command line interface."""

import argparse
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path

from tool_agent_lab.knowledge.ingest import PolicyCatalog
from tool_agent_lab.settings import load_settings
from tool_agent_lab.tools.contracts import (
    PolicyHit, PolicySearchResult, ReadPolicyArgs, SearchPolicyArgs,
)

K1 = 1.2
B = 0.75
TOKEN_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+|[a-z0-9_]+")
CHINESE_TERMS = frozenset("""
退款 补偿 确认 提案 订单 金额 实付 全额 延迟 物流 数字 商品 实物 损坏
破损 碎裂 核实 收货 到货 期限 超期 七天 小时 政策 版本 生效 有效
时间 条款 人工 转人工 澄清 缺失 查询 操作 账本 结果 状态 成功 失败
重复 幂等 超时 响应 丢失 不确定 授权 拒绝 批准 修改 参数 运费 币种
整数 固定 发放 平台 原因 事实 记录
""".split())
STOP_WORDS = frozenset("请 请问 我 我的 的 了 是 是否 可以 怎么 如何".split())
CHINESE_WORDS = CHINESE_TERMS | STOP_WORDS
MAX_WORD_LENGTH = max(map(len, CHINESE_WORDS))


def _chinese_tokens(run: str) -> list[str]:
    tokens, unknown = [], []

    def flush_unknown() -> None:
        text = "".join(unknown)
        if len(text) == 1:
            tokens.append(text)
        else:
            tokens.extend(text[index:index + 2] for index in range(len(text) - 1))
        unknown.clear()

    index = 0
    while index < len(run):
        word = next((run[index:index + size] for size in range(min(MAX_WORD_LENGTH, len(run) - index), 0, -1)
                     if run[index:index + size] in CHINESE_WORDS), None)
        if word is None:
            unknown.append(run[index])
            index += 1
            continue
        flush_unknown()
        if word not in STOP_WORDS:
            tokens.append(word)
        index += len(word)
    flush_unknown()
    return tokens


def tokenize(text: str) -> tuple[str, ...]:
    """Segment after-sales terms, retain unknown bigrams and normalize identifiers."""
    tokens = []
    for run in TOKEN_RUN.findall(unicodedata.normalize("NFKC", text).lower()):
        if "\u3400" <= run[0] <= "\u9fff":
            tokens.extend(_chinese_tokens(run))
        else:
            tokens.append(run)
    return tuple(tokens)


class PolicySearch:
    def __init__(self, catalog: PolicyCatalog, *, business_time: datetime) -> None:
        if business_time.tzinfo is None or business_time.utcoffset() is None:
            raise ValueError("business_time must include a timezone")
        self.catalog = catalog
        self.business_time = business_time
        self._entries = []
        for record in catalog.documents:
            if not record.effective_from <= business_time < record.effective_to:
                continue
            for section in record.sections:
                document = record.document(section.section_id)
                counts = Counter(tokenize(f"{document.title}\n{document.text}"))
                self._entries.append((document, counts, counts.total()))

    @classmethod
    def from_data_dir(cls, data_dir: str | Path, *, business_time: datetime | None = None) -> "PolicySearch":
        data_dir = Path(data_dir)
        if business_time is None:
            spec = json.loads((data_dir / "spec.json").read_text(encoding="utf-8"))
            business_time = datetime.fromisoformat(spec["business_time"])
        return cls(PolicyCatalog.from_file(data_dir / "policies.json"), business_time=business_time)

    def search_policy(self, args: SearchPolicyArgs) -> PolicySearchResult:
        terms = set(tokenize(args.query))
        categories = {args.category, "all"} if args.category in {"general_goods", "digital_goods"} else {args.category}
        candidates = [(document, counts, length) for document, counts, length in self._entries
                      if (args.category is None or document.category in categories)
                      and (args.version is None or document.reference.version == args.version)]
        if not terms or not candidates:
            return PolicySearchResult(query=args.query, hits=())
        total = len(candidates)
        average_length = sum(length for _, _, length in candidates) / total
        document_frequency = Counter(term for _, counts, _ in candidates for term in counts)
        hits = []
        for document, counts, length in candidates:
            score = 0.0
            for term in sorted(terms & counts.keys()):
                frequency = counts[term]
                df = document_frequency[term]
                idf = math.log1p((total - df + 0.5) / (df + 0.5))
                normalizer = K1 * (1 - B + B * length / average_length)
                score += idf * frequency * (K1 + 1) / (frequency + normalizer)
            if score > 0:
                hits.append(PolicyHit(
                    reference=document.reference, title=document.title,
                    excerpt=document.text, score=score,
                ))
        hits.sort(key=lambda hit: (
            -hit.score, hit.reference.policy_id, hit.reference.version, hit.reference.section,
        ))
        return PolicySearchResult(query=args.query, hits=tuple(hits[:args.limit]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="Defaults to the application business_data_dir")
    parser.add_argument("--business-time", type=datetime.fromisoformat, help="Timezone-aware ISO business time")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="Search effective policy sections")
    search.add_argument("query")
    search.add_argument("--category")
    search.add_argument("--version")
    search.add_argument("--limit", type=int, default=5)
    read = commands.add_parser("read", help="Read an explicitly versioned policy or section")
    read.add_argument("policy_id")
    read.add_argument("version")
    read.add_argument("--section")
    args = parser.parse_args(argv)
    try:
        data_dir = args.data_dir if args.data_dir is not None else load_settings().business_data_dir
        if args.command == "read":
            result = PolicyCatalog.from_file(data_dir / "policies.json").read_policy(ReadPolicyArgs(
                policy_id=args.policy_id, version=args.version, section=args.section,
            ))
        else:
            result = PolicySearch.from_data_dir(data_dir, business_time=args.business_time).search_policy(
                SearchPolicyArgs(query=args.query, category=args.category, version=args.version, limit=args.limit)
            )
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))
    sys.stdout.reconfigure(encoding="utf-8")
    print(result.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
