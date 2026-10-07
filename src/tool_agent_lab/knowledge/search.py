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


def tokenize(text: str) -> tuple[str, ...]:
    """Use Chinese characters/bigrams and lowercase Latin identifiers/numbers."""
    tokens = []
    for run in TOKEN_RUN.findall(unicodedata.normalize("NFKC", text).lower()):
        if "\u3400" <= run[0] <= "\u9fff":
            tokens.extend(run)
            tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
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
        self._document_frequency = Counter()
        for record in catalog.documents:
            if not record.effective_from <= business_time < record.effective_to:
                continue
            for section in record.sections:
                document = record.document(section.section_id)
                counts = Counter(tokenize(f"{document.title}\n{document.text}"))
                self._entries.append((document, counts, counts.total()))
                self._document_frequency.update(counts.keys())
        self._average_length = (
            sum(length for _, _, length in self._entries) / len(self._entries)
            if self._entries else 0.0
        )

    @classmethod
    def from_data_dir(cls, data_dir: str | Path, *, business_time: datetime | None = None) -> "PolicySearch":
        data_dir = Path(data_dir)
        if business_time is None:
            spec = json.loads((data_dir / "spec.json").read_text(encoding="utf-8"))
            business_time = datetime.fromisoformat(spec["business_time"])
        return cls(PolicyCatalog.from_file(data_dir / "policies.json"), business_time=business_time)

    def search_policy(self, args: SearchPolicyArgs) -> PolicySearchResult:
        terms = set(tokenize(args.query))
        hits = []
        total = len(self._entries)
        categories = {args.category, "all"} if args.category in {"general_goods", "digital_goods"} else {args.category}
        for document, counts, length in self._entries:
            if args.category is not None and document.category not in categories:
                continue
            if args.version is not None and document.reference.version != args.version:
                continue
            score = 0.0
            for term in sorted(terms & counts.keys()):
                frequency = counts[term]
                df = self._document_frequency[term]
                idf = math.log1p((total - df + 0.5) / (df + 0.5))
                normalizer = K1 * (1 - B + B * length / self._average_length)
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
