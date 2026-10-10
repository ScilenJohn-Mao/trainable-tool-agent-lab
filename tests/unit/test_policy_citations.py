from datetime import UTC, datetime, timedelta

import pytest

from tool_agent_lab.knowledge.citations import CitationError, validate_citation
from tool_agent_lab.knowledge.ingest import PolicyCatalog
from tool_agent_lab.knowledge.search import PolicySearch
from tool_agent_lab.schemas.actions import PolicyReference
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.tools.contracts import ReadPolicyArgs, SearchPolicyArgs


DATA = PROJECT_ROOT / "data/business/v1"
CATALOG = PolicyCatalog.from_file(DATA / "policies.json")
CLOCK = datetime.fromisoformat("2026-09-17T12:00:00+08:00")


def amount_document(version="mock-policy-v1"):
    return CATALOG.read_policy(ReadPolicyArgs(
        policy_id="P-DELAY-AMOUNT", version=version, section="amount",
    ))


@pytest.mark.parametrize("record", CATALOG.documents, ids=lambda r: f"{r.policy_id}:{r.version}")
def test_exact_versions_sections_and_marked_full_text_resolve(record):
    for section in ("/", *(item.section_id for item in record.sections)):
        document = record.document(section)
        restored = PolicyReference.model_validate_json(document.reference.model_dump_json())
        assert validate_citation(CATALOG, restored, excerpt=document.text) == document


@pytest.mark.parametrize(("changes", "code"), [
    ({"policy_id": "missing"}, "citation_not_found"),
    ({"version": "missing"}, "citation_not_found"),
    ({"section": "missing"}, "citation_not_found"),
    ({"version": "mock-policy-v0"}, "citation_interval_mismatch"),
    ({"effective_from": datetime.fromisoformat("2026-08-01T00:00:00+08:00")}, "citation_interval_mismatch"),
    ({"effective_to": datetime.fromisoformat("2026-11-01T00:00:00+08:00")}, "citation_interval_mismatch"),
])
def test_missing_or_altered_reference_is_not_replaced(changes, code):
    reference = PolicyReference.model_validate(amount_document().reference.model_dump() | changes)
    with pytest.raises(CitationError, match=code) as caught:
        validate_citation(CATALOG, reference)
    assert caught.value.code == code


def test_equivalent_utc_reference_is_preserved_as_catalog_reference():
    reference = amount_document().reference
    utc_reference = reference.model_copy(update={
        "effective_from": reference.effective_from.astimezone(UTC),
        "effective_to": reference.effective_to.astimezone(UTC),
    })
    assert validate_citation(CATALOG, utc_reference).reference is not utc_reference
    assert validate_citation(CATALOG, utc_reference) == amount_document()


@pytest.mark.parametrize("version", ["mock-policy-v0", "mock-policy-v1", "mock-policy-v2"])
def test_half_open_applicability_is_optional_for_historical_display(version):
    document = amount_document(version)
    ref = document.reference
    assert validate_citation(CATALOG, ref) == document
    for clock in (ref.effective_from, ref.effective_from.astimezone(UTC), ref.effective_to - timedelta(microseconds=1)):
        assert validate_citation(CATALOG, ref, business_time=clock) == document
    for clock in (ref.effective_from - timedelta(microseconds=1), ref.effective_to):
        with pytest.raises(CitationError, match="citation_not_active"):
            validate_citation(CATALOG, ref, business_time=clock)
    if version != "mock-policy-v1":
        with pytest.raises(CitationError, match="citation_not_active"):
            validate_citation(CATALOG, ref, business_time=CLOCK)


def test_naive_business_time_is_not_interpreted_as_local_time():
    with pytest.raises(ValueError, match="timezone"):
        validate_citation(CATALOG, amount_document().reference, business_time=CLOCK.replace(tzinfo=None))


@pytest.mark.parametrize(("document_category", "category", "allowed"), [
    ("general_goods", "general_goods", True),
    ("general_goods", "digital_goods", False),
    ("general_goods", "all", False),
    ("digital_goods", "digital_goods", True),
    ("digital_goods", "general_goods", False),
    ("all", "general_goods", True),
    ("all", "digital_goods", True),
    ("all", "all", True),
    ("all", "unknown", False),
])
def test_category_applicability_matches_search(document_category, category, allowed):
    record = next(r for r in CATALOG.documents if r.category == document_category and r.version == "mock-policy-v1")
    ref = record.reference(record.sections[0].section_id)
    if allowed:
        assert validate_citation(CATALOG, ref, category=category, business_time=CLOCK).category == document_category
    else:
        with pytest.raises(CitationError, match="citation_category_mismatch"):
            validate_citation(CATALOG, ref, category=category, business_time=CLOCK)


@pytest.mark.parametrize("excerpt_kind", ["changed_amount", "other_version", "other_section", "empty", "trimmed"])
def test_excerpt_must_equal_the_original_referenced_text(excerpt_kind):
    document = amount_document()
    candidates = {
        "changed_amount": document.text.replace("500", "800"),
        "other_version": amount_document("mock-policy-v2").text,
        "other_section": CATALOG.documents[0].document(CATALOG.documents[0].sections[0].section_id).text,
        "empty": "", "trimmed": document.text[:-1],
    }
    assert candidates[excerpt_kind] != document.text
    with pytest.raises(CitationError, match="citation_excerpt_mismatch"):
        validate_citation(CATALOG, document.reference, excerpt=candidates[excerpt_kind])


def test_actual_search_hits_validate_with_exact_excerpt_category_and_clock():
    engine = PolicySearch.from_data_dir(DATA)
    hits = engine.search_policy(SearchPolicyArgs(query="延迟补偿500分", category="general_goods")).hits
    assert hits
    for hit in hits:
        assert validate_citation(
            engine.catalog, hit.reference, excerpt=hit.excerpt,
            business_time=engine.business_time, category="general_goods",
        ).text == hit.excerpt


def test_business_rule_reference_cannot_be_resolved_as_a_document_citation():
    from tool_agent_lab.business.rules import BusinessRules

    ref = BusinessRules.from_file(DATA / "spec.json").reference("request_refund")
    with pytest.raises(CitationError, match="citation_not_found"):
        validate_citation(CATALOG, ref)
