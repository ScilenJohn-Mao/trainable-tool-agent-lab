# Model client

The client supports local Transformers + PEFT inference, explicitly scripted
mock replies and optional OpenAI-compatible `POST /v1/chat/completions` servers.
Mock, HTTP and configuration inspection import no inference or training
libraries. The client executes no tools or business writes.

## Local model assets and direct inference

The application deployment target uses the same Transformers + PEFT direct
inference code on native Windows and Linux. The worker loads local weights and
an optional adapter through Python; no remote model endpoint, Ollama or vLLM is
required for application inference. Browser-to-API HTTP/SSE remains local to the
machine. Select `configs/models/qwen3b-local.yaml` for direct inference. The HTTP
examples below describe the optional compatibility interface.

Store the complete official Qwen2.5-3B-Instruct repository at
`models/base/Qwen2.5-3B-Instruct/` under the project root, including configuration,
tokenizer, weight shards and their index. Store exported PEFT adapters at
`models/adapters/<version>/`, including `adapter_config.json` and
`adapter_model.safetensors`. Pin the same base revision, tokenizer and chat
template as training. An adapter is not a complete base model.

Root `/models/` is Git-ignored and outside the source archive allowlist;
`configs/models/` contains application configuration and remains packaged.
Model assets are transferred separately and preserved across source updates.
See [the model directory instructions](../README.md#本地模型文件).

From the project root, prepare the separate environment on Windows:

```powershell
uv sync --project inference --locked --python .venv/Scripts/python.exe --cache-dir .uv-cache
uv run --project inference --no-sync --cache-dir .uv-cache python -m tool_agent_lab.agent.model_client --config configs/models/qwen3b-local.yaml --message "Check ORD-1001" --with-tools
```

On Linux, use `--python python3.12` for the sync; the invocation is otherwise
identical. The universal lock selects Windows AMD64/Linux x86_64 CUDA 12.8
wheels, pins torch 2.9.1, Transformers 4.57.6, PEFT 0.18.1, Accelerate 1.12.0 and
bitsandbytes 0.49.2, and aligns shared dependencies with the application lock.
It creates `inference/.venv` and installs the shared application editable from
`..`; it does not modify the lightweight `.venv`. Dependency installation needs
prepared downloads or network access; model generation reads local files only.
The runtime has not yet been installed or GPU-tested on either platform.

Local fields are `base_path`, optional `adapter_path`, `device` (`cuda:0` by
default), `precision` (`4bit`, `float16` or `float32`) and `context_tokens`.
All support the same `TTAL_MODEL_*` overrides. Paths resolve against the source
project root; absolute paths also work. CPU requires an explicit `float32`
configuration and enough RAM. There is no automatic CPU, HTTP or mock fallback.

The base tokenizer supplies the chat template; adapter tokenizers are not
substituted. Every pretrained load uses a local path and `local_files_only=True`;
remote model code is disabled. The 4-bit configuration uses NF4 with double
quantization. Each client caches one model and serializes generation calls.
`max_tokens` becomes `max_new_tokens`; input plus that output budget must fit
`context_tokens`. Overflow raises an error without silently truncating history.
Generation uses the model's EOS IDs. `timeout_seconds` is Transformers'
best-effort `max_time`, checked between generation steps; model loading is not
included. Replies expose `stop`, `tool_calls`, `length` or `timeout` and actual
input/output token counts. These counts are not training logprobs.

Qwen `<tool_call>` envelopes become the shared `ModelToolCall` representation,
including unique call IDs and original argument JSON. Complete malformed
envelopes raise an error; unfinished budget-limited output remains text with a
non-success finish reason. The caller must check that reason, validate through
the shared tool contract, and use the normal executor. A request to refund does
not itself authorize or execute a refund.

For another turn, append `reply.message.as_message()` and tool messages with
matching `tool_call_id` values before calling `generate` again. The local
adapter converts argument strings to objects for the Qwen template without
mutating caller history. The CLI accepts a UTF-8 JSON array of message history:

```powershell
uv run --project inference --no-sync --cache-dir .uv-cache python -m tool_agent_lab.agent.model_client --config configs/models/qwen3b-local.yaml --messages-file path/to/history.json --with-tools
```

The history path is relative to the terminal working directory. Set
`TTAL_MODEL_ADAPTER_PATH=models/adapters/<version>` in the process or `.env` to
load a transferred PEFT adapter; start a new client/process after changing it.
Keep the exact training base revision and files. This loader does not prove that
an arbitrary ART checkpoint is a compatible exported PEFT adapter.

The CLI and Python boundary are implemented. Actual 3B tool behavior, GPU memory,
speed and Windows/Linux adapter compatibility still require real model runs;
the Agent graph, worker and UI are not connected yet.

API references: [Qwen's tokenizer template](https://huggingface.co/Qwen/Qwen2.5-3B-Instruct/blob/main/tokenizer_config.json),
[Transformers chat templates](https://huggingface.co/docs/transformers/v4.57.3/chat_templating),
[PyTorch wheels](https://pytorch.org/get-started/previous-versions/) and
[bitsandbytes installation](https://huggingface.co/docs/bitsandbytes/installation).

Direct inference uses a separate Python 3.12/uv environment with PyTorch,
Transformers, PEFT, Accelerate and bitsandbytes. The lightweight application
environment remains usable for mock/API/MCP checks. Model loading must use local
paths and offline options; missing files must not trigger remote inference or a
mock substitute. Windows and Linux require compatible platform-specific GPU
packages even though application source and model assets are shared.

Only Linux RL training depends on fixed-version ART. Its dedicated vLLM runtime
uses the matching ART `vllm_runtime/pyproject.toml`, `uv.lock` and `setup.sh` in
its own uv environment, separate from the training and application environments.
ART may communicate with that runtime over same-machine HTTP. Once dependencies,
models and required compilation resources are prepared, training is validated
without external services; cloud logging/storage and external model judges are
disabled. Exported PEFT adapters can be moved to Windows without ART or vLLM.

## Inspect and call

Run from the project directory:

```powershell
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.agent.model_client
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.agent.model_client --message "Hello"
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.agent.model_client --config configs/models/qwen3b.yaml
```

The mock CLI returns a labelled connectivity example. It does not interpret the
user request or claim any business success. In Python, supply `mock_responses`
explicitly; each call consumes one reply and exhaustion raises `ValueError`.
Each task or simulation should use its own mock client.

To call an already running Linux server, set overrides in the current terminal
or the project's optional `.env` file:

```powershell
$env:TTAL_MODEL_BASE_URL = "http://your-model-server:8001/v1"
uv run --no-sync --cache-dir .uv-cache python -m tool_agent_lab.agent.model_client --config configs/models/qwen3b.yaml --message "Please ask me for an order number." --with-tools
```

Replace the example address with your real endpoint. The shipped loopback address
is useful with a local tunnel; it does not start or install a model server.
`name` must match the model name exposed by the server. Set `TTAL_MODEL_NAME`
when the server uses a different served-model name. Remove session overrides with
`Remove-Item Env:TTAL_MODEL_BASE_URL` and `Remove-Item Env:TTAL_MODEL_NAME` when
finished, or restore their prior values if already set.

For a server requiring authentication, provide its key through
`TTAL_MODEL_API_KEY`; do not put credentials in YAML or source control. The key
is omitted from configuration output and object representations. An absent key
means no Authorization header. The optional `api_key_env` selects another
credential variable; it does not contain a key.

Configuration precedence is process `TTAL_MODEL_*` variables > optional `.env` >
selected YAML. `--env-file` selects a different dotenv file. YAML/dotenv paths are
anchored at the source project root even when invoked from another directory.
Reading settings writes no files and does not alter environment variables.
Fields are `provider`, `name`, `version`, `base_url`, `max_tokens`, `temperature`,
`timeout_seconds`, and `api_key_env`. The version is an explicit application
label, not proof of the remote weights' identity. The response also retains the
server's actual response ID, model name, finish reason and reported usage.

## Python interface

```python
import asyncio

from tool_agent_lab.agent.model_client import (
    AssistantMessage, FunctionCall, ModelClient, ModelToolCall, load_model_config,
)
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS, ToolName

async def example():
    client = ModelClient(load_model_config(), mock_responses=[AssistantMessage(
        tool_calls=(ModelToolCall(
            id="call-order", function=FunctionCall(
                name="get_order", arguments='{"order_id":"ORD-1001"}',
            ),
        ),),
    )])
    reply = await client.generate(
        [{"role": "user", "content": "Please check ORD-1001"}],
        tools=[TOOL_CONTRACTS[ToolName.GET_ORDER]],
    )
    print(reply.model_dump_json(indent=2))

asyncio.run(example())
```

`generate(messages, tools=...)` makes a single non-streaming request. Tool schemas
come from the shared `ToolContract` objects; the client sends `tool_choice=auto`
when tools are supplied. Messages include system/user/assistant/tool history;
`reply.message.as_message()` preserves tool calls for the next request. Callers
must pair tool results with the returned call IDs.

Tool argument strings remain exactly as returned, including invalid JSON or
unknown tool names. The caller parses and validates them through shared tool
contracts and decides the next step. Model output grants no approval or trusted
execution context. It must never be interpreted as a committed refund record.

`max_tokens` and `temperature` are sent to the server; HTTP requests use the
configured timeout. HTTP errors, transport/timeouts, malformed JSON or malformed
completion envelopes propagate to the caller. There are no retries or fallback
to mock. Truncated replies retain `finish_reason=length` for caller handling.
An injected `httpx.AsyncClient` remains owned by the caller; otherwise each call
opens and closes its own client.

The Linux server must expose chat completions with parsed tool calls. For Qwen2.5,
vLLM documents automatic tool choice and the Hermes parser. Check flags against
the installed server version; this application does not change the GPU stack.
See [vLLM tool calling](https://docs.vllm.ai/en/latest/features/tool_calling/).

This interface does not run an Agent graph, consume human decisions, implement
recovery, or collect the token/logprob records needed for training.
