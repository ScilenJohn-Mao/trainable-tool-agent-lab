"""Check manually annotated retrieval inputs and their policy evidence."""

import json
from collections import Counter
from datetime import datetime

import pytest

from scripts.package_project import collect_sources
from tool_agent_lab.knowledge.citations import validate_citation
from tool_agent_lab.knowledge.ingest import PolicyCatalog
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.tools.contracts import ReadPolicyArgs, SearchPolicyArgs


QUERIES_PATH = PROJECT_ROOT / "data/retrieval/queries.jsonl"
QUERIES = [json.loads(line) for line in QUERIES_PATH.read_text(encoding="utf-8").splitlines()]
CATALOG = PolicyCatalog.from_file(PROJECT_ROOT / "data/business/v1/policies.json")


def test_query_set_has_unique_inputs_and_covers_policy_topics_and_versions():
    assert len(QUERIES) == 50
    assert len({case["query_id"] for case in QUERIES}) == 50
    assert len({case["query"] for case in QUERIES}) == 50
    positives = [case for case in QUERIES if case["expected"]["relevant_sections"]]
    assert len(positives) == 44
    refs = [ref for case in positives for ref in case["expected"]["relevant_sections"]]
    assert len(refs) == 46
    assert Counter(ref["version"] for ref in refs) == {
        "mock-policy-v1": 42, "mock-policy-v0": 2, "mock-policy-v2": 2,
    }
    assert {(ref["policy_id"], ref["version"]) for ref in refs} == {
        (record.policy_id, record.version) for record in CATALOG.documents
    }
    assert {case["category"] for case in QUERIES} == {
        "general_goods", "digital_goods", "all", "unknown_goods", None,
    }


@pytest.mark.parametrize("case", QUERIES, ids=lambda case: case["query_id"])
def test_query_annotation_resolves_to_applicable_original_evidence(case):
    assert set(case) == {"query_id", "query", "category", "version", "business_time", "tags", "expected"}
    args = SearchPolicyArgs(query=case["query"], category=case["category"], version=case["version"])
    assert args.query == case["query"]
    clock = datetime.fromisoformat(case["business_time"])
    assert clock.utcoffset() is not None
    assert case["tags"] and all(isinstance(tag, str) and tag.strip() for tag in case["tags"])
    assert set(case["expected"]) == {"relevant_sections", "rationale"}
    assert case["expected"]["rationale"].strip()
    refs = case["expected"]["relevant_sections"]
    assert len({(r["policy_id"], r["version"], r["section"]) for r in refs}) == len(refs)
    for ref in refs:
        assert set(ref) == {"policy_id", "version", "section", "evidence"}
        document = CATALOG.read_policy(ReadPolicyArgs(
            policy_id=ref["policy_id"], version=ref["version"], section=ref["section"],
        ))
        assert ref["evidence"].strip() and ref["evidence"] in document.text
        assert validate_citation(
            CATALOG, document.reference, business_time=clock, category=args.category,
        ) == document
        if args.version is not None:
            assert ref["version"] == args.version
    assert ("negative" in case["tags"]) == (not refs)


def test_filter_negatives_have_no_candidate_and_unrelated_question_has_no_business_label():
    negatives = [case for case in QUERIES if not case["expected"]["relevant_sections"]]
    assert len(negatives) == 6
    for case in negatives:
        if "unrelated" in case["tags"]:
            assert case["query"] == "退市" and case["version"] is None
            continue
        clock = datetime.fromisoformat(case["business_time"])
        category = case["category"]
        if category in ("general_goods", "digital_goods"):
            categories = {category, "all"}
        else:
            categories = {"all"} if category == "all" else set()
        candidates = [record for record in CATALOG.documents if (
            record.effective_from <= clock < record.effective_to
            and record.category in categories
            and (case["version"] is None or record.version == case["version"])
        )]
        assert not candidates, case["query_id"]


def test_amount_boundary_and_combined_labels_keep_independent_business_facts():
    cases = {case["query_id"]: case for case in QUERIES}

    def refs(query_id):
        return cases[query_id]["expected"]["relevant_sections"]

    assert "12900 分" in refs("RQ-003")[0]["evidence"]
    assert "恰好 7 天" in refs("RQ-005")[0]["evidence"]
    assert "24 小时" in refs("RQ-009")[0]["evidence"]
    assert "500 分 CNY" in refs("RQ-011")[0]["evidence"]
    assert "5 天" in refs("RQ-041")[0]["evidence"]
    assert "300 分 CNY" in refs("RQ-042")[0]["evidence"]
    assert "10 天" in refs("RQ-043")[0]["evidence"]
    assert "800 分 CNY" in refs("RQ-044")[0]["evidence"]
    assert len(refs("RQ-016")) == 3


def test_source_inventory_includes_queries_and_usage_without_building_an_archive():
    bundle = collect_sources(PROJECT_ROOT, PROJECT_ROOT / "artifacts/packages")
    for relative in ("data/retrieval/queries.jsonl", "data/retrieval/README.md"):
        assert bundle.files[relative] == (PROJECT_ROOT / relative).read_bytes()
