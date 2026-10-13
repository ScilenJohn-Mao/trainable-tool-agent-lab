"""Training configuration and fixed source/runtime identities without heavy imports."""

from pathlib import Path
import os

from pydantic import Field, model_validator
import yaml

from tool_agent_lab.agent.config import load_agent_config
from tool_agent_lab.agent.model_client import ModelConfig
from tool_agent_lab.schemas.common import ContractModel
from tool_agent_lab.schemas.scenarios import ExperimentConfig
from tool_agent_lab.settings import PROJECT_ROOT

ART_REVISION = "12162f241320f2343f1df162e834dc5ae03f740d"
ART_VERSION = "0.5.20"


class TrainingConfig(ContractModel):
    name: str
    dataset: Path = Path("data/scenarios/v1")
    split: str = "train"
    seed: int = 20261013
    base_model: Path
    base_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    output_dir: Path = Path("artifacts/training")
    steps: int = Field(gt=0)
    max_sampling_groups: int = Field(default=10, gt=0)
    group_size: int = Field(default=2, ge=2)
    groups_per_step: int = Field(default=1, ge=1, le=1)
    active_generation: int = Field(default=1, ge=1, le=1)
    sequence_tokens: int = Field(default=8192, ge=1, le=8192)
    output_tokens: int = Field(default=2048, ge=1, le=2048)
    max_decisions: int = Field(default=12, ge=1, le=12)
    temperature: float = Field(default=1, gt=0, le=2)
    timeout_seconds: float = Field(default=600, gt=0)
    learning_rate: float = Field(default=0.000005, gt=0)
    loss_fn: str = "cispo"
    lora_rank: int = Field(default=16, gt=0)
    lora_alpha: int = Field(default=32, gt=0)
    gpu_memory_utilization: float = Field(default=.55, gt=0, lt=1)
    art_revision: str = ART_REVISION
    art_version: str = ART_VERSION
    runtime_version: str = "0.1.0"
    vllm_version: str = "0.25.1+cu129"
    agent_config: Path = Path("configs/agents/default.yaml")

    @model_validator(mode="after")
    def check_identity(self):
        if self.split != "train" or self.loss_fn != "cispo":
            raise ValueError("Training uses only train split and the fixed cispo loss")
        if self.art_revision != ART_REVISION or self.art_version != ART_VERSION:
            raise ValueError("ART source identity differs from the fixed integration")
        if self.runtime_version != "0.1.0" or self.vllm_version != "0.25.1+cu129":
            raise ValueError("Runtime identity differs from the fixed runtime lock")
        if self.output_tokens >= self.sequence_tokens:
            raise ValueError("Output budget must leave room for the prompt")
        if self.max_sampling_groups < self.steps:
            raise ValueError("Sampling budget must cover the requested updates")
        if self.base_model.name != "Qwen2.5-7B-Instruct":
            raise ValueError("Training requires Qwen2.5-7B-Instruct")
        return self

    def experiment(self):
        agent = load_agent_config(self.agent_config)
        agent = agent.model_copy(update={"budgets": agent.budgets.model_copy(update={
            "max_tokens": self.output_tokens, "max_decisions": self.max_decisions})})
        return ExperimentConfig(model=ModelConfig(provider="http", name="Qwen2.5-7B-Instruct",
            version=self.name, base_url="http://127.0.0.1:8000/v1", max_tokens=self.output_tokens,
            temperature=self.temperature, timeout_seconds=self.timeout_seconds, context_tokens=self.sequence_tokens), agent=agent)


def load_config(path):
    config = TrainingConfig.model_validate(yaml.safe_load((PROJECT_ROOT / path).read_text(encoding="utf-8")))
    return config.model_copy(update={k: (PROJECT_ROOT / getattr(config, k)).resolve()
                                    for k in ("dataset", "base_model", "output_dir", "agent_config")})


def offline_environment():
    """Set process-local offline settings before ART, model or tracing imports."""
    os.environ.update(ART_VLLM_RUNTIME_CUDA_PROFILE="cuda12",
                      HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", WANDB_MODE="disabled", WEAVE_DISABLED="true",
                      LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    for key in ("WANDB_API_KEY", "WEAVE_API_KEY", "LANGSMITH_API_KEY"):
        os.environ.pop(key, None)
