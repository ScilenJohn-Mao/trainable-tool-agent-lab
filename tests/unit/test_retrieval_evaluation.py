"""Check retrieval metric denominators, exact versions and citation failures."""

import json
import subprocess
import sys

import pytest

from scripts import evaluate_retrieval as evaluation
from tool_agent_lab.knowledge.ingest import PolicyCatalog
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.tools.contracts import PolicyHit, PolicySearchResult


CATALOG = PolicyCatalog.from_file(PROJECT_ROOT / "data/business/v1/policies.json")
CURRENT = [record.document(section.section_id) for record in CATALOG.documents
           if record.version == "mock-policy-v1" and record.category == "all"
           for section in record.sections][:3]


def case(query_id, expected):
    return {"query_id": query_id, "query": query_id, "category": "all", "version": "mock-policy-v1",
            "business_time": "2026-09-17T12:00:00+08:00", "tags": ["diagnostic"],
            "expected": {"relevant_sections": [
                document.reference.model_dump(mode="json") | {"evidence": document.text}
                for document in expected
            ]}}


def hit(document):
    return PolicyHit(reference=document.reference, title=document.title, excerpt=document.text, score=1)


def fixed_results(monkeypatch, results):
    class FixedSearch:
        def __init__(self, catalog, *, business_time):
            assert catalog is CATALOG
            assert business_time.isoformat() == "2026-09-17T12:00:00+08:00"

        def search_policy(self, args):
            assert set(args.model_dump()) == {"query", "category", "version", "limit"}
            return PolicySearchResult(query=args.query, hits=tuple(results[args.query][:args.limit]))

    monkeypatch.setattr(evaluation, "PolicySearch", FixedSearch)


def test_macro_micro_and_negative_denominators_are_separate(monkeypatch):
    fixed_results(monkeypatch, {"multi": [hit(CURRENT[0])], "single": [hit(CURRENT[1])], "none": []})
    report = evaluation.evaluate([case("multi", CURRENT), case("single", [CURRENT[1]]), case("none", [])],
                                 CATALOG, [1, 3])
    summary = report["summary"]
    assert (summary["positive_queries"], summary["negative_queries"], summary["relevant_sections"]) == (2, 1, 4)
    assert summary["at_k"]["1"]["macro_recall"] == pytest.approx(2 / 3)
    assert summary["at_k"]["1"]["micro_recall"] == 0.5
    assert summary["at_k"]["1"]["negative_empty_rate"] == 1
    assert report["queries"][2]["at_k"]["1"]["recall"] is None
    assert len(report["queries"][0]["at_k"]["3"]["missing_sections"]) == 2


def test_same_policy_and_section_with_wrong_version_is_not_a_match(monkeypatch):
    wrong = hit(CURRENT[0]).model_copy(update={"reference": CURRENT[0].reference.model_copy(
        update={"version": "absent-version"})})
    fixed_results(monkeypatch, {"wrong": [wrong]})
    report = evaluation.evaluate([case("wrong", [CURRENT[0]])], CATALOG, [1])
    assert report["summary"]["at_k"]["1"]["macro_recall"] == 0
    assert report["queries"][0]["hits"][0]["citation_error"] == "citation_not_found"
    assert len(report["version_filter_violations"]) == 1
    assert report["returned_versions"] == {"absent-version": 1}


def test_invalid_excerpt_and_nonempty_negative_remain_in_report(monkeypatch):
    bad = hit(CURRENT[0]).model_copy(update={"excerpt": "altered policy text"})
    fixed_results(monkeypatch, {"negative": [bad]})
    report = evaluation.evaluate([case("negative", [])], CATALOG, [1])
    metrics = report["summary"]["at_k"]["1"]
    assert metrics["macro_recall"] is None and metrics["micro_recall"] is None
    assert metrics["negative_empty_rate"] == 0
    assert metrics["returned_citations"] == 1 and metrics["valid_citations"] == 0
    assert report["queries"][0]["hits"][0]["citation_error"] == "citation_excerpt_mismatch"


def test_real_query_set_counts_and_current_historical_future_versions():
    cases = [json.loads(line) for line in (PROJECT_ROOT / "data/retrieval/queries.jsonl").read_text(
        encoding="utf-8").splitlines()]
    report = evaluation.evaluate(cases, CATALOG, [1, 3, 5, 10])
    assert report["summary"]["queries"] == 50
    assert report["summary"]["relevant_sections"] == 46
    assert {version: group["positive_queries"] for version, group in report["by_expected_version"].items()} == {
        "mock-policy-v0": 2, "mock-policy-v1": 40, "mock-policy-v2": 2,
    }
    assert report["version_filter_violations"] == []
    for metrics in report["summary"]["at_k"].values():
        assert metrics["valid_citations"] == metrics["returned_citations"]
    recalls = [report["summary"]["at_k"][str(k)]["macro_recall"] for k in report["k_values"]]
    assert recalls == sorted(recalls)


def test_cli_saves_utf8_report_and_source_fingerprints(tmp_path):
    output = tmp_path / "reports/baseline.json"
    process = subprocess.run([sys.executable, "scripts/evaluate_retrieval.py", "--k", "1", "5", "--output", str(output)],
                             cwd=PROJECT_ROOT, capture_output=True, encoding="utf-8", check=True)
    summary = json.loads(process.stdout)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert summary["summary"] == report["summary"]
    assert report["k_values"] == [1, 5]
    assert report["inputs"]["queries"]["sha256"] == "9bd3de103527088ecd3133339bbd06bfc108c176af30a9e6db4e6ff27ef4268b"
    assert all(len(source["sha256"]) == 64 for source in report["inputs"].values())
    assert report["retrieval"] == {"algorithm": "section_bm25", "k1": 1.2, "b": 0.75}
    assert report["queries"][0]["query"].startswith("箱子")
