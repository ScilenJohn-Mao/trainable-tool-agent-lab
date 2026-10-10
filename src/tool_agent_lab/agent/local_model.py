"""Direct offline Qwen inference with a local base model and optional PEFT adapter."""

from __future__ import annotations

import copy
import json
import re
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from tool_agent_lab.agent.model_client import (
    AssistantMessage, FunctionCall, ModelConfig, ModelReply, ModelToolCall,
    chat_tool_definitions,
)
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.tools.contracts import ToolContract


def _argument_text(envelope: str) -> str:
    """Keep the model's original argument JSON instead of reserializing it."""
    decoder = json.JSONDecoder()
    position = envelope.index("{") + 1
    while True:
        position += len(envelope[position:]) - len(envelope[position:].lstrip())
        key, position = decoder.raw_decode(envelope, position)
        position = envelope.index(":", position) + 1
        position += len(envelope[position:]) - len(envelope[position:].lstrip())
        start = position
        value, position = decoder.raw_decode(envelope, position)
        if key == "arguments":
            return value if isinstance(value, str) else envelope[start:position]
        position += len(envelope[position:]) - len(envelope[position:].lstrip())
        if envelope[position] == "}":
            raise ValueError("Tool call is missing arguments")
        position += 1


def parse_qwen_output(text: str, *, truncated: bool = False) -> AssistantMessage:
    """Decode Qwen tool envelopes; shared tool contracts validate arguments later."""
    matches = list(re.finditer(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL))
    envelopes = [re.sub(r"^(?:<tool_call>\s*)+", "", match.group(1).strip()) for match in matches]
    remaining = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL).strip()
    if "<tool_call>" in remaining or "</tool_call>" in remaining:
        if truncated:
            return AssistantMessage(content=text)
        raise ValueError("Incomplete Qwen tool call envelope")
    calls = []
    if len(envelopes) == 1:
        envelope = envelopes[0]
        value = json.loads(envelope)
        # Small Qwen models can wrap the graph's control reply in a tool delimiter.
        if isinstance(value, dict) and "name" not in value and value.get("kind") in ("ask_user", "final"):
            return AssistantMessage(content=envelope)
    if not matches and remaining.startswith("JSON "):
        candidate = remaining[5:].strip()
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(value, dict) and value.get("kind") in ("ask_user", "final"):
                return AssistantMessage(content=candidate)
    for envelope in envelopes:
        value = json.loads(envelope)
        calls.append(ModelToolCall(
            id=f"call-{uuid4().hex}",
            function=FunctionCall(name=value["name"], arguments=_argument_text(envelope)),
        ))
    return AssistantMessage(content=remaining or (None if calls else ""), tool_calls=tuple(calls))


def template_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI argument strings to objects expected by the Qwen template."""
    result = copy.deepcopy(list(messages))
    for message in result:
        if message["role"] == "assistant":
            message["content"] = message.get("content") or ""
            for call in message.get("tool_calls", ()):
                arguments = call["function"]["arguments"]
                if isinstance(arguments, str):
                    call["function"]["arguments"] = json.loads(arguments)
    return result


class LocalModel:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self._model = None
        self._tokenizer = None
        self._lock = threading.Lock()

    def _load(self) -> None:
        base = (PROJECT_ROOT / Path(self.config.base_path).expanduser()).resolve()
        adapter = (
            (PROJECT_ROOT / self.config.adapter_path.expanduser()).resolve()
            if self.config.adapter_path else None
        )
        for directory in (base, adapter):
            if directory is not None and not directory.is_dir():
                raise FileNotFoundError(f"Local model directory does not exist: {directory}")
        for directory, filename in (
            (base, "config.json"), (base, "tokenizer_config.json"), (adapter, "adapter_config.json"),
        ):
            if directory is not None and not (directory / filename).is_file():
                raise FileNotFoundError(f"Required local model file does not exist: {directory / filename}")
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        except ImportError as error:
            raise RuntimeError("Run local inference in the separately synced inference/ environment") from error

        dtype = torch.float32 if self.config.precision == "float32" else torch.float16
        if self.config.device.startswith("cuda:") and not torch.cuda.is_available():
            raise RuntimeError("Configured CUDA device is unavailable; no CPU or mock fallback")
        tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
        if not tokenizer.chat_template:
            raise ValueError("The base tokenizer must supply its matching Qwen chat template")
        options = dict(
            local_files_only=True, trust_remote_code=False, use_safetensors=True,
            dtype=dtype, device_map={"": self.config.device},
        )
        if self.config.precision == "4bit":
            options["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=dtype, bnb_4bit_use_double_quant=True,
            )
        model = AutoModelForCausalLM.from_pretrained(base, **options)
        if adapter is not None:
            from peft import PeftModel

            model = PeftModel.from_pretrained(model, adapter, local_files_only=True, is_trainable=False)
        model.eval()
        self._tokenizer, self._model = tokenizer, model

    def generate(
        self, messages: Sequence[dict[str, Any]], *, tools: Sequence[ToolContract] = (),
    ) -> ModelReply:
        # One model instance serves one request at a time, including its first load.
        with self._lock:
            if self._model is None:
                self._load()
            import torch
            from torch.nn.attention import SDPBackend, sdpa_kernel

            inputs = self._tokenizer.apply_chat_template(
                template_messages(messages), tools=chat_tool_definitions(tools) or None,
                tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors="pt",
            )
            prompt_tokens = inputs["input_ids"].shape[-1]
            if prompt_tokens + self.config.max_tokens > self.config.context_tokens:
                raise ValueError("Input plus output budget exceeds context_tokens; history was not truncated")
            inputs = inputs.to(self._model.device)
            options = dict(
                max_new_tokens=self.config.max_tokens, do_sample=self.config.temperature > 0,
                max_time=self.config.timeout_seconds, pad_token_id=self._tokenizer.pad_token_id,
            )
            if self.config.temperature > 0:
                options["temperature"] = self.config.temperature
            # cuDNN handles grouped attention on Windows builds without Flash Attention;
            # prefer it to the quadratic math kernel for longer tool histories.
            with torch.inference_mode(), sdpa_kernel(
                [SDPBackend.FLASH_ATTENTION, SDPBackend.EFFICIENT_ATTENTION,
                 SDPBackend.CUDNN_ATTENTION, SDPBackend.MATH], set_priority=True,
            ):
                output = self._model.generate(**inputs, **options)
            tokens = output[0][prompt_tokens:].tolist()
            eos = self._model.generation_config.eos_token_id
            eos_ids = eos if isinstance(eos, list) else [eos]
            stopped = bool(tokens and tokens[-1] in eos_ids)
            truncated = not stopped
            # Keep tool delimiters even if the tokenizer marks them as special tokens.
            text = self._tokenizer.decode(tokens[:-1] if stopped else tokens, skip_special_tokens=False)
            message = parse_qwen_output(text, truncated=truncated)
            finish = "length" if len(tokens) >= self.config.max_tokens and truncated else (
                "timeout" if truncated else "tool_calls" if message.tool_calls else "stop"
            )
            return ModelReply(
                model_version=self.config.version, response_model=self.config.name,
                response_id=f"local-{uuid4().hex}", message=message, finish_reason=finish,
                usage={"prompt_tokens": prompt_tokens, "completion_tokens": len(tokens),
                       "total_tokens": prompt_tokens + len(tokens)},
            )
