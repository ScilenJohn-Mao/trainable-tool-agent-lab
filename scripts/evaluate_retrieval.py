"""Measure annotated policy retrieval without a model or business database."""

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from tool_agent_lab.knowledge import citations, ingest, search
from tool_agent_lab.knowledge.citations import CitationError, validate_citation
from tool_agent_lab.knowledge.ingest import PolicyCatalog
from tool_agent_lab.knowledge.search import B, K1, PolicySearch
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.tools.contracts import SearchPolicyArgs


def reference_key(reference: dict) -> tuple[str, str, str]:
    return reference["policy_id"], reference["version"], reference["section"]


def summarize(rows: list[dict], ks: list[int]) -> dict:
    positives = [row for row in rows if row["expected_sections"]]
    negatives = [row for row in rows if not row["expected_sections"]]
    relevant_count = sum(len(row["expected_sections"]) for row in positives)
    metrics = {}
    for k in ks:
        positive_results = [row["at_k"][str(k)] for row in positives]
        hits = [hit for row in rows for hit in row["hits"][:k]]
        empty = sum(not row["hits"][:k] for row in negatives)
        metrics[str(k)] = {
            "macro_recall": (sum(item["recall"] for item in positive_results) / len(positives)
                             if positives else None),
            "micro_recall": (sum(item["matched_count"] for item in positive_results) / relevant_count
                             if relevant_count else None),
            "matched_sections": sum(item["matched_count"] for item in positive_results),
            "full_coverage_queries": sum(not item["missing_sections"] for item in positive_results),
            "negative_empty_queries": empty,
            "negative_empty_rate": empty / len(negatives) if negatives else None,
            "returned_citations": len(hits),
            "valid_citations": sum(hit["citation_error"] is None for hit in hits),
        }
    return {"queries": len(rows), "positive_queries": len(positives),
            "negative_queries": len(negatives), "relevant_sections": relevant_count, "at_k": metrics}


def evaluate(cases: list[dict], catalog: PolicyCatalog, ks: list[int]) -> dict:
    ks = sorted(set(ks))
    rows = []
    for case in cases:
        clock = datetime.fromisoformat(case["business_time"])
        args = SearchPolicyArgs(query=case["query"], category=case["category"],
                                version=case["version"], limit=max(ks))
        result = PolicySearch(catalog, business_time=clock).search_policy(args)
        expected = case["expected"]["relevant_sections"]
        hits = []
        for rank, hit in enumerate(result.hits, start=1):
            citation_error = None
            try:
                validate_citation(catalog, hit.reference, excerpt=hit.excerpt,
                                  business_time=clock, category=args.category)
            except CitationError as error:
                citation_error = error.code
            hits.append({"rank": rank, **hit.model_dump(mode="json"),
                         "citation_error": citation_error})
        at_k = {}
        for k in ks:
            retrieved = {reference_key(hit["reference"]) for hit in hits[:k]}
            matched = [ref for ref in expected if reference_key(ref) in retrieved]
            missing = [ref for ref in expected if reference_key(ref) not in retrieved]
            at_k[str(k)] = {"matched_count": len(matched), "missing_sections": missing,
                            "recall": len(matched) / len(expected) if expected else None}
        rows.append({"query_id": case["query_id"], "query": args.query,
                     "category": args.category, "version_filter": args.version,
                     "business_time": case["business_time"], "tags": case["tags"],
                     "expected_sections": expected, "hits": hits, "at_k": at_k})
    versions = sorted({ref["version"] for row in rows for ref in row["expected_sections"]})
    tags = sorted({tag for row in rows for tag in row["tags"]})
    version_filter_violations = [
        {"query_id": row["query_id"], "rank": hit["rank"], "reference": hit["reference"]}
        for row in rows for hit in row["hits"]
        if row["version_filter"] is not None and hit["reference"]["version"] != row["version_filter"]
    ]
    return {
        "k_values": ks, "summary": summarize(rows, ks),
        "by_expected_version": {
            version: summarize([row for row in rows if any(
                ref["version"] == version for ref in row["expected_sections"])], ks)
            for version in versions
        },
        "by_tag": {tag: summarize([row for row in rows if tag in row["tags"]], ks) for tag in tags},
        "returned_versions": dict(sorted(Counter(
            hit["reference"]["version"] for row in rows for hit in row["hits"]
        ).items())),
        "version_filter_violations": version_filter_violations, "queries": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, default=PROJECT_ROOT / "data/retrieval/queries.jsonl")
    parser.add_argument("--policies", type=Path, default=PROJECT_ROOT / "data/business/v1/policies.json")
    parser.add_argument("--k", nargs="+", type=int, choices=range(1, 11), default=[1, 3, 5, 10])
    parser.add_argument("--output", type=Path, help="Save the full UTF-8 JSON report; print a summary")
    args = parser.parse_args(argv)
    cases = [json.loads(line) for line in args.queries.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = evaluate(cases, PolicyCatalog.from_file(args.policies), args.k)
    files = {"queries": args.queries, "policies": args.policies,
             "search": Path(search.__file__), "citations": Path(citations.__file__),
             "ingest": Path(ingest.__file__), "evaluator": Path(__file__)}
    report["inputs"] = {name: {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                        for name, path in files.items()}
    report["retrieval"] = {"algorithm": "section_bm25", "k1": K1, "b": B}
    sys.stdout.reconfigure(encoding="utf-8")
    if args.output is None:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"report": str(args.output), "summary": report["summary"],
                          "by_expected_version": report["by_expected_version"],
                          "version_filter_violations": report["version_filter_violations"]},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
