"""Load versioned policy documents and resolve stable section references."""

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from tool_agent_lab.schemas.actions import PolicyReference
from tool_agent_lab.schemas.common import ContractModel, NonEmptyStr
from tool_agent_lab.tools.contracts import PolicyDocument, ReadPolicyArgs


class PolicySection(ContractModel):
    section_id: NonEmptyStr
    text: NonEmptyStr


class PolicySource(ContractModel):
    file: NonEmptyStr
    pointer: str
    rule_ids: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]


class PolicyRecord(ContractModel):
    policy_id: NonEmptyStr
    version: NonEmptyStr
    title: NonEmptyStr
    category: Literal["general_goods", "digital_goods", "all"]
    effective_from: AwareDatetime
    effective_to: AwareDatetime
    source: PolicySource
    sections: Annotated[tuple[PolicySection, ...], Field(min_length=1)]

    def reference(self, section: str) -> PolicyReference:
        return PolicyReference(
            policy_id=self.policy_id, version=self.version, section=section,
            effective_from=self.effective_from, effective_to=self.effective_to,
        )

    @model_validator(mode="after")
    def check_sections_and_interval(self) -> Self:
        self.reference("/")
        ids = [section.section_id for section in self.sections]
        if len(set(ids)) != len(ids) or "/" in ids:
            raise ValueError("section IDs must be unique; '/' is reserved for the full document")
        return self

    def document(self, section: str = "/") -> PolicyDocument:
        if section == "/":
            text = "\n\n".join(f"[{item.section_id}]\n{item.text}" for item in self.sections)
        else:
            item = next((item for item in self.sections if item.section_id == section), None)
            if item is None:
                raise KeyError((self.policy_id, self.version, section))
            text = item.text
        return PolicyDocument(
            reference=self.reference(section), title=self.title, category=self.category, text=text,
        )


class PolicyCatalog(ContractModel):
    format_version: Literal[1]
    documents: Annotated[tuple[PolicyRecord, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def check_document_ids(self) -> Self:
        ids = [(item.policy_id, item.version) for item in self.documents]
        if len(set(ids)) != len(ids):
            raise ValueError("policy ID and version must uniquely identify a document")
        return self

    @classmethod
    def from_file(cls, path: str | Path) -> Self:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def read_policy(self, args: ReadPolicyArgs) -> PolicyDocument:
        record = next((item for item in self.documents
                       if (item.policy_id, item.version) == (args.policy_id, args.version)), None)
        if record is None:
            raise KeyError((args.policy_id, args.version))
        return record.document(args.section or "/")
