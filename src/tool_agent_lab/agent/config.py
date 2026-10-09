"""Load the shared agent prompt and finite execution budgets."""

from pathlib import Path

import yaml
from pydantic import Field

from tool_agent_lab.schemas.common import ContractModel, NonEmptyStr
from tool_agent_lab.settings import PROJECT_ROOT


class AgentBudgets(ContractModel):
    max_decisions: int = Field(default=12, gt=0)
    max_tokens: int = Field(default=512, gt=0)
    max_tool_calls_per_turn: int = Field(default=7, gt=0)
    max_context_bytes: int = Field(default=24000, gt=0)
    max_tool_output_bytes: int = Field(default=4096, gt=0)


class AgentConfig(ContractModel):
    version: NonEmptyStr
    prompt: NonEmptyStr
    budgets: AgentBudgets


def load_agent_config(path: str | Path = "configs/agents/default.yaml") -> AgentConfig:
    values = yaml.safe_load((PROJECT_ROOT / path).read_text(encoding="utf-8"))
    budgets = yaml.safe_load((PROJECT_ROOT / values["budgets_path"]).read_text(encoding="utf-8"))
    return AgentConfig(version=values["version"], budgets=budgets,
                       prompt=(PROJECT_ROOT / values["prompt_path"]).read_text(encoding="utf-8").strip())
