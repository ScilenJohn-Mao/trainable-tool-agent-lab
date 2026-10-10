"""Exercise real policy import, lexical ranking and citation reads."""

import copy
import json
import math
import os
import subprocess
import sys
from datetime import datetime

import pytest
from pydantic import ValidationError

from scripts.package_project import collect_sources
from tool_agent_lab.knowledge.ingest import PolicyCatalog
from tool_agent_lab.knowledge.search import PolicySearch, tokenize
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.tools.contracts import (
    TOOL_CONTRACTS, PolicyDocument, PolicySearchResult, ReadPolicyArgs, SearchPolicyArgs,
)

DATA = PROJECT_ROOT / "data/business/v1"
RAW = json.loads((DATA / "policies.json").read_text(encoding="utf-8"))
CLOCK = datetime.fromisoformat("2026-09-17T12:00:00+08:00")


@pytest.fixture
def engine() -> PolicySearch:
    return PolicySearch.from_data_dir(DATA)


def test_import_retains_versions_sources_and_full_documents(engine: PolicySearch) -> None:
    catalog = engine.catalog
    assert len(catalog.documents) == 24
    assert engine.business_time == CLOCK
    assert PolicyCatalog.model_validate_json(catalog.model_dump_json()) == catalog
    for raw in RAW["documents"]:
        full = catalog.read_policy(ReadPolicyArgs(policy_id=raw["policy_id"], version=raw["version"]))
        assert full.reference.section == "/"
        assert full.category == raw["category"] and full.title == raw["title"]
        assert all(f'[{item["section_id"]}]\n{item["text"]}' in full.text for item in raw["sections"])
        assert catalog.read_policy(ReadPolicyArgs(
            policy_id=raw["policy_id"], version=raw["version"], section="/",
        )) == full
    assert catalog.documents[0].source.file == "spec.json"
    assert catalog.documents[-1].source.pointer == "/versions/1"


@pytest.mark.parametrize("raw", RAW["documents"], ids=lambda item: f'{item["policy_id"]}:{item["version"]}')
def test_each_section_is_readable_by_exact_reference(engine: PolicySearch, raw: dict) -> None:
    for section in raw["sections"]:
        result = engine.catalog.read_policy(ReadPolicyArgs(
            policy_id=raw["policy_id"], version=raw["version"], section=section["section_id"],
        ))
        assert result.text == section["text"]
        assert result.reference.effective_from.isoformat() == raw["effective_from"]
        assert result.reference.effective_to.isoformat() == raw["effective_to"]
        payload = {"call_id": "read-1", "status": "ok", "data": result.model_dump(mode="json")}
        assert TOOL_CONTRACTS["read_policy"].parse_result(payload, call_id="read-1").data == result


@pytest.mark.parametrize("change", ["duplicate_document", "duplicate_section", "reserved_section",
                                     "empty_sections", "empty_text", "naive_time", "bad_interval", "bad_format"])
def test_import_rejects_ambiguous_or_invalid_citations(change: str) -> None:
    raw = copy.deepcopy(RAW)
    first = raw["documents"][0]
    if change == "duplicate_document":
        raw["documents"].append(first)
    elif change == "duplicate_section":
        first["sections"].append(first["sections"][0])
    elif change == "reserved_section":
        first["sections"][0]["section_id"] = "/"
    elif change == "empty_sections":
        first["sections"] = []
    elif change == "empty_text":
        first["sections"][0]["text"] = " "
    elif change == "naive_time":
        first["effective_from"] = "2026-09-01T00:00:00"
    elif change == "bad_interval":
        first["effective_to"] = first["effective_from"]
    else:
        raw["format_version"] = 2
    with pytest.raises(ValidationError):
        PolicyCatalog.model_validate(raw)


@pytest.mark.parametrize("args", [
    ReadPolicyArgs(policy_id="missing", version="mock-policy-v1"),
    ReadPolicyArgs(policy_id="P-REFUND-ELIGIBILITY", version="missing"),
    ReadPolicyArgs(policy_id="P-REFUND-ELIGIBILITY", version="mock-policy-v1", section="missing"),
])
def test_missing_reference_never_falls_back_to_another_document(engine: PolicySearch, args: ReadPolicyArgs) -> None:
    with pytest.raises(KeyError):
        engine.catalog.read_policy(args)


@pytest.mark.parametrize(("query", "policy_id"), [
    ("退款金额实付12900分", "P-REFUND-AMOUNT"),
    ("延迟补偿券固定500分", "P-DELAY-AMOUNT"),
    ("数字商品转人工", "P-DIGITAL-HANDOFF"),
    ("响应丢失结果不确定", "P-UNCERTAIN-OUTCOME"),
    ("找不到订单先澄清", "P-ORDER-CLARIFICATION"),
    ("修改提案重新确认", "P-PROPOSAL-CHANGES"),
])
def test_chinese_queries_retrieve_expected_policy_and_readable_hits(
    engine: PolicySearch, query: str, policy_id: str,
) -> None:
    result = engine.search_policy(SearchPolicyArgs(query=query, limit=5))
    assert policy_id in {hit.reference.policy_id for hit in result.hits}
    assert result.hits and all(hit.score > 0 and hit.reference.version == "mock-policy-v1" for hit in result.hits)
    assert [hit.score for hit in result.hits] == sorted((hit.score for hit in result.hits), reverse=True)
    for hit in result.hits:
        original = engine.catalog.read_policy(ReadPolicyArgs(
            policy_id=hit.reference.policy_id, version=hit.reference.version, section=hit.reference.section,
        ))
        assert original.text == hit.excerpt and original.reference == hit.reference
    payload = {"call_id": "search-1", "status": "ok", "data": result.model_dump(mode="json")}
    assert TOOL_CONTRACTS["search_policy"].parse_result(payload, call_id="search-1").data == result


@pytest.mark.parametrize(("clock", "version"), [
    ("2026-07-31T23:59:59+08:00", None),
    ("2026-08-01T00:00:00+08:00", "mock-policy-v0"),
    ("2026-08-31T23:59:59+08:00", "mock-policy-v0"),
    ("2026-09-01T00:00:00+08:00", "mock-policy-v1"),
    ("2026-09-30T23:59:59+08:00", "mock-policy-v1"),
    ("2026-10-01T00:00:00+08:00", "mock-policy-v2"),
    ("2026-11-01T00:00:00+08:00", None),
])
def test_search_obeys_half_open_business_time_intervals(clock: str, version: str | None) -> None:
    engine = PolicySearch.from_data_dir(DATA, business_time=datetime.fromisoformat(clock))
    result = engine.search_policy(SearchPolicyArgs(query="退款补偿", limit=10))
    assert {hit.reference.version for hit in result.hits} == ({version} if version else set())


def test_filters_limits_no_match_and_naive_clock(engine: PolicySearch) -> None:
    general = engine.search_policy(SearchPolicyArgs(query="退款补偿确认", category="general_goods", limit=10))
    digital = engine.search_policy(SearchPolicyArgs(query="数字商品转人工确认", category="digital_goods", limit=10))
    for result, category in [(general, "general_goods"), (digital, "digital_goods")]:
        categories = {engine.catalog.read_policy(ReadPolicyArgs(
            policy_id=hit.reference.policy_id, version=hit.reference.version,
        )).category for hit in result.hits}
        assert categories == {category, "all"}
    for args in [SearchPolicyArgs(query="退款", version="mock-policy-v0"),
                 SearchPolicyArgs(query="退款", version="mock-policy-v2"),
                 SearchPolicyArgs(query="退款", category="unknown"),
                 SearchPolicyArgs(query="zzzz_unique_missing"), SearchPolicyArgs(query="!?，。")]:
        assert engine.search_policy(args).hits == ()
    args = SearchPolicyArgs(query="退款", version="mock-policy-v1", limit=1)
    assert len(engine.search_policy(args).hits) == 1
    with pytest.raises(ValueError, match="timezone"):
        PolicySearch(engine.catalog, business_time=CLOCK.replace(tzinfo=None))


def test_bm25_frequency_length_normalization_and_deterministic_ties() -> None:
    records = []
    for policy_id, text in [("P-A", "refund refund damage"), ("P-B", "refund delay")]:
        record = copy.deepcopy(RAW["documents"][0])
        record.update(policy_id=policy_id, title="Policy", sections=[{"section_id": "clause", "text": text}])
        records.append(record)
    engine = PolicySearch(PolicyCatalog(format_version=1, documents=records), business_time=CLOCK)
    result = engine.search_policy(SearchPolicyArgs(query="refund"))
    assert [hit.reference.policy_id for hit in result.hits] == ["P-A", "P-B"]
    # Independent counts: two sections, lengths 4 and 3, refund frequencies 2 and 1.
    expected = math.log(1.2) * 2 * 2.2 / (2 + 1.2 * (0.25 + 0.75 * 4 / 3.5))
    assert result.hits[0].score == pytest.approx(expected)
    records[1]["sections"][0]["text"] = records[0]["sections"][0]["text"]
    tied = PolicySearch(PolicyCatalog(format_version=1, documents=records[::-1]), business_time=CLOCK)
    assert [hit.reference.policy_id for hit in tied.search_policy(SearchPolicyArgs(query="refund")).hits] == ["P-A", "P-B"]


def test_tokenization_preserves_business_words_latin_and_amounts() -> None:
    assert tokenize("退款 CNY paid_amount_minor ５００") == ("退款", "cny", "paid_amount_minor", "500")


def test_chinese_segmentation_keeps_terms_unknown_bigrams_and_negation() -> None:
    assert tokenize("请问我的退款金额是否可以确认") == ("退款", "金额", "确认")
    assert tokenize("超时响应丢失不确定转人工") == ("超时", "响应", "丢失", "不确定", "转人工")
    assert tokenize("售后风控") == ("售后", "后风", "风控")
    assert tokenize("不退款") == ("不", "退款")
    assert tokenize("请问！？") == ()


def test_unrelated_word_does_not_retrieve_refund_policies_by_one_character(engine: PolicySearch) -> None:
    assert engine.search_policy(SearchPolicyArgs(query="退市")).hits == ()
    assert engine.search_policy(SearchPolicyArgs(query="请问是否可以")).hits == ()
    expected = engine.search_policy(SearchPolicyArgs(query="退款金额实付12900分"))
    natural = engine.search_policy(SearchPolicyArgs(query="请问我的退款金额实付12900分是否可以"))
    assert natural.hits == expected.hits


@pytest.mark.parametrize("excluded_by", ["category", "version", "time"])
def test_excluded_documents_do_not_change_filtered_bm25_scores(excluded_by: str) -> None:
    eligible = copy.deepcopy(RAW["documents"][0])
    eligible.update(policy_id="P-ELIGIBLE", title="Policy", sections=[{"section_id": "clause", "text": "refund damage"}])
    excluded = copy.deepcopy(eligible)
    excluded.update(policy_id="P-EXCLUDED", sections=[{"section_id": "clause", "text": "refund " * 20}])
    if excluded_by == "category":
        excluded["category"] = "digital_goods"
    elif excluded_by == "version":
        excluded["version"] = "other-version"
    else:
        excluded["effective_to"] = CLOCK.isoformat()
    args = SearchPolicyArgs(query="refund", category="general_goods", version="mock-policy-v1")
    alone = PolicySearch(PolicyCatalog(format_version=1, documents=[eligible]), business_time=CLOCK)
    mixed = PolicySearch(PolicyCatalog(format_version=1, documents=[eligible, excluded]), business_time=CLOCK)
    assert mixed.search_policy(args).hits == alone.search_policy(args).hits


@pytest.mark.parametrize(("clock", "version", "category", "expected_ids"), [
    ("2026-09-01T00:00:00+08:00", "mock-policy-v1", "digital_goods", {"P-DIGITAL-HANDOFF"}),
    ("2026-09-17T04:00:00+00:00", "mock-policy-v1", "digital_goods", {"P-DIGITAL-HANDOFF"}),
    ("2026-09-17T12:00:00+08:00", "mock-policy-v1", "all", set()),
    ("2026-10-01T00:00:00+08:00", "mock-policy-v1", "digital_goods", set()),
    ("2026-09-17T12:00:00+08:00", "mock-policy-v0", "digital_goods", set()),
    ("2026-09-17T12:00:00+08:00", "mock-policy-v2", "digital_goods", set()),
    ("2026-09-17T12:00:00+08:00", "mock-policy-v1", "unknown", set()),
])
def test_category_time_and_version_filters_apply_together(clock, version, category, expected_ids) -> None:
    engine = PolicySearch.from_data_dir(DATA, business_time=datetime.fromisoformat(clock))
    result = engine.search_policy(SearchPolicyArgs(query="数字", category=category, version=version, limit=10))
    assert {hit.reference.policy_id for hit in result.hits} == expected_ids
    assert all(hit.reference.version == version for hit in result.hits)


@pytest.mark.parametrize(("arguments", "model"), [
    (["search", "延迟补偿券", "--limit", "2"], PolicySearchResult),
    (["read", "P-DELAY-AMOUNT", "mock-policy-v1", "--section", "amount"], PolicyDocument),
    (["read", "P-REFUND-ELIGIBILITY", "mock-policy-v0"], PolicyDocument),
])
def test_cli_runs_from_other_working_directory_with_utf8(tmp_path, arguments: list[str], model: type) -> None:
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
    completed = subprocess.run(
        [sys.executable, "-m", "tool_agent_lab.knowledge.search", *arguments], cwd=tmp_path,
        env=env, capture_output=True, encoding="utf-8", check=True,
    )
    assert model.model_validate_json(completed.stdout)
    assert completed.stderr == ""


def test_cli_missing_section_returns_nonzero(capsys) -> None:
    from tool_agent_lab.knowledge.search import main

    with pytest.raises(SystemExit) as error:
        main(["read", "P-REFUND-ELIGIBILITY", "mock-policy-v1", "--section", "missing"])
    assert error.value.code == 2 and "missing" in capsys.readouterr().err


def test_search_resources_enter_source_package(tmp_path) -> None:
    paths = set(collect_sources(PROJECT_ROOT, tmp_path / "packages").files)
    assert {"src/tool_agent_lab/knowledge/ingest.py", "src/tool_agent_lab/knowledge/search.py",
            "data/business/v1/policies.json", "docs/policies.md"} <= paths
