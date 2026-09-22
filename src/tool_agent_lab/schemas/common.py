"""Shared field constraints; no runtime or training dependencies."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
