"""Call a remote chat model or consume explicitly scripted mock replies."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Self

import httpx
import yaml
from dotenv import dotenv_values
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, model_validator

from tool_agent_lab.schemas.common import ContractModel, NonEmptyStr
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS, ToolContract


class ModelConfig(ContractModel):
    provider: Literal["mock", "http"]
    name: NonEmptyStr
    version: NonEmptyStr
    base_url: AnyHttpUrl | None = None
    max_tokens: int = Field(default=512, gt=0)
    temperature: float = Field(default=0, ge=0, le=2, allow_inf_nan=False)
    timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    api_key_env: NonEmptyStr = "TTAL_MODEL_API_KEY"
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def require_http_endpoint(self) -> Self:
        if self.provider == "http" and self.base_url is None:
            raise ValueError("HTTP models require base_url ending at the API root, such as /v1")
        return self


def load_model_config(
    config_path: str | Path = "configs/models/mock.yaml",
    *,
    env_file: str | Path | None = None,
) -> ModelConfig:
    """Read project-relative YAML and local overrides without mutating the environment."""
    path = (PROJECT_ROOT / Path(config_path).expanduser()).resolve()
    with path.open(encoding="utf-8") as stream:
        values = yaml.safe_load(stream)
    if not isinstance(values, dict):
        raise ValueError(f"Model configuration must be a YAML mapping: {path}")
    dotenv_path = (PROJECT_ROOT / Path(env_file or ".env").expanduser()).resolve()
    overrides = {}
    if env_file is not None or dotenv_path.exists():
        with dotenv_path.open(encoding="utf-8") as stream:
            overrides.update(dotenv_values(stream=stream, interpolate=False))
    overrides.update(os.environ)
    for field in ModelConfig.model_fields.keys() - {"api_key"}:
        variable = f"TTAL_MODEL_{field.upper()}"
        if variable in overrides:
            values[field] = overrides[variable]
    # Credentials are read only from the selected environment variable or dotenv file.
    if "api_key" in values:
        raise ValueError("Use api_key_env instead of storing an API key in model YAML")
    values["api_key"] = overrides.get(values.get("api_key_env", "TTAL_MODEL_API_KEY")) or None
    return ModelConfig.model_validate(values)


class FunctionCall(ContractModel):
    name: NonEmptyStr
    arguments: str


class ModelToolCall(ContractModel):
    id: NonEmptyStr
    type: Literal["function"] = "function"
    function: FunctionCall


class AssistantMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: tuple[ModelToolCall, ...] = ()

    @model_validator(mode="after")
    def require_output(self) -> Self:
        if self.content is None and not self.tool_calls:
            raise ValueError("Assistant output must contain text or tool calls")
        return self

    def as_message(self) -> dict[str, Any]:
        """Return the assistant message for the next model request's history."""
        message = self.model_dump(mode="json")
        if not self.tool_calls:
            del message["tool_calls"]
        return message


class ModelReply(ContractModel):
    model_version: NonEmptyStr
    response_model: NonEmptyStr
    response_id: NonEmptyStr
    message: AssistantMessage
    finish_reason: NonEmptyStr
    usage: dict[str, Any] | None = None


class _Choice(BaseModel):
    message: AssistantMessage
    finish_reason: NonEmptyStr


class _Completion(BaseModel):
    id: NonEmptyStr
    model: NonEmptyStr
    choices: list[_Choice] = Field(min_length=1, max_length=1)
    usage: dict[str, Any] | None = None


def chat_tool_definitions(tools: Sequence[ToolContract]) -> list[dict[str, Any]]:
    """Expose the existing shared tool arguments in the chat completion format."""
    return [
        {"type": "function", "function": {
            "name": tool.name.value,
            "description": tool.description,
            "parameters": tool.argument_model.model_json_schema(),
        }}
        for tool in tools
    ]


class ModelClient:
    def __init__(
        self,
        config: ModelConfig,
        *,
        mock_responses: Sequence[AssistantMessage] = (),
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._mock_responses = iter(mock_responses)
        self._mock_calls = 0
        self._http_client = http_client

    async def generate(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[ToolContract] = (),
    ) -> ModelReply:
        """Generate one assistant turn; tool execution belongs to the caller."""
        if self.config.provider == "mock":
            message = next(self._mock_responses, None)
            if message is None:
                raise ValueError("Mock replies exhausted; supply explicit mock_responses")
            self._mock_calls += 1
            return ModelReply(
                model_version=self.config.version, response_model=self.config.name,
                response_id=f"mock-{self._mock_calls}", message=message,
                finish_reason="tool_calls" if message.tool_calls else "stop",
            )

        payload = {
            "model": self.config.name, "messages": list(messages),
            "max_tokens": self.config.max_tokens, "temperature": self.config.temperature,
            "stream": False, "n": 1,
        }
        if tools:
            payload.update(tools=chat_tool_definitions(tools), tool_choice="auto")
        url = str(self.config.base_url).rstrip("/") + "/chat/completions"
        headers = {}
        if self.config.api_key is not None:
            headers["Authorization"] = f"Bearer {self.config.api_key.get_secret_value()}"
        if self._http_client is None:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url, json=payload, headers=headers, timeout=self.config.timeout_seconds,
                    follow_redirects=False,
                )
        else:
            response = await self._http_client.post(
                url, json=payload, headers=headers, timeout=self.config.timeout_seconds,
                follow_redirects=False,
            )
        response.raise_for_status()
        completion = _Completion.model_validate(response.json())
        choice = completion.choices[0]
        return ModelReply(
            model_version=self.config.version, response_model=completion.model,
            response_id=completion.id, message=choice.message,
            finish_reason=choice.finish_reason, usage=completion.usage,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/models/mock.yaml")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--message", help="Make one model call instead of inspecting configuration")
    parser.add_argument("--with-tools", action="store_true", help="Include the seven shared tool schemas")
    args = parser.parse_args(argv)
    config = load_model_config(args.config, env_file=args.env_file)
    if args.message is None:
        output = config.model_dump(mode="json")
    else:
        client = ModelClient(config, mock_responses=[AssistantMessage(
            content="Mock model ready. No business operation was executed.",
        )])
        reply = asyncio.run(client.generate(
            [{"role": "user", "content": args.message}],
            tools=tuple(TOOL_CONTRACTS.values()) if args.with_tools else (),
        ))
        output = reply.model_dump(mode="json")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
