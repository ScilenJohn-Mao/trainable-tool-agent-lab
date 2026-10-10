"""Resolve policy citations against their exact version and original section text."""

from datetime import datetime

from tool_agent_lab.knowledge.ingest import PolicyCatalog
from tool_agent_lab.schemas.actions import PolicyReference
from tool_agent_lab.tools.contracts import PolicyDocument, ReadPolicyArgs


class CitationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_citation(
    catalog: PolicyCatalog,
    reference: PolicyReference,
    *,
    excerpt: str | None = None,
    business_time: datetime | None = None,
    category: str | None = None,
) -> PolicyDocument:
    """Return the original document; optionally require current applicability.

    Without business_time, historical citations can be resolved for display.
    An excerpt must equal the referenced section (or marked full document).
    This checks provenance, not whether the text supports a business decision.
    """
    try:
        document = catalog.read_policy(ReadPolicyArgs(
            policy_id=reference.policy_id, version=reference.version, section=reference.section,
        ))
    except KeyError as error:
        raise CitationError("citation_not_found") from error
    if reference != document.reference:
        raise CitationError("citation_interval_mismatch")
    if excerpt is not None and excerpt != document.text:
        raise CitationError("citation_excerpt_mismatch")
    if business_time is not None:
        if business_time.tzinfo is None or business_time.utcoffset() is None:
            raise ValueError("business_time must include a timezone")
        if not reference.effective_from <= business_time < reference.effective_to:
            raise CitationError("citation_not_active")
    if category is not None:
        if category in ("general_goods", "digital_goods"):
            categories = {category, "all"}
        else:
            categories = {"all"} if category == "all" else set()
        if document.category not in categories:
            raise CitationError("citation_category_mismatch")
    return document
