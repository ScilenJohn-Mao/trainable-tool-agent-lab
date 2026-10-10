# Independent worker

Run from the source project root using the application environment for mock
execution, or the independently installed inference environment for local models.
The worker uses the same Agent graph, seven MCP tools and business services.
It processes one task at a time and retains the real local model client/cache
across tasks and human waiting points.

```powershell
uv run --no-sync --cache-dir .uv-cache python -m apps.worker.main --help
uv run --no-sync --cache-dir .uv-cache python -m apps.worker.main --mock-responses artifacts/runtime/mock-replies.json --once
uv run --project inference --no-sync --cache-dir .uv-cache python -m apps.worker.main --model-config configs/models/qwen3b-local.yaml
```

The local model command requires installed inference dependencies and complete
model files. It loads them directly in this worker, without a remote model
endpoint. Missing files/dependencies raise an error; there is no mock fallback.

`--once` handles one ready task through its next waiting point or final result;
it prints a JSON result and exits. With no ready task it prints `{"status":"idle"}`.
Omit `--once` to poll continuously; Ctrl+C stops the process. `--poll-interval`
defaults to 0.5 seconds. The API and worker share `model_config_file` and
`agent_config_file` from application settings. `--model-config` and
`--agent-config` override these for the worker only; configure the API with the
same files when using an override. Paths are anchored to the source project root,
including paths to mock replies.

An explicit mock script is a JSON array of assistant messages, for example:

```json
[
  {"role":"assistant","content":"{\"kind\":\"ask_user\",\"question\":\"Which order?\"}"},
  {"role":"assistant","content":"{\"kind\":\"final\",\"summary\":\"Input received.\"}"}
]
```

The same script is replayed independently for each task. After a normal waiting
point/reopen it starts at that task's saved model-decision count. This is a
development substitute, not evidence that a real model chose the actions.

Create queued tasks through the shared TaskService with the worker's exact
model/config versions. For example, inside the same configured application:

```python
from tool_agent_lab.agent.config import load_agent_config
from tool_agent_lab.agent.model_client import load_model_config
from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.runtime.task_service import TaskService
from tool_agent_lab.schemas.tasks import TaskCreate
from tool_agent_lab.settings import load_settings

settings = load_settings()
rules = BusinessRules.from_file(settings.business_data_dir / "spec.json")
tasks = TaskService(settings.app_db_path, business_time=rules.business_time,
                    model_version=load_model_config(settings.model_config_file).version,
                    config_version=load_agent_config(settings.agent_config_file).version)
task = tasks.create(TaskCreate(user_message="My item is damaged"),
                    owner_id=settings.dev_owner_id)
```

Initialize the application database first. Order queries/writes additionally
need the existing business fixtures. Use `scripts/seed_demo.py` with a new
isolated database and point an application config's runtime_dir at its parent;
never reset the active application database for a demonstration.

The worker only picks the configured owner and compatible model/config version.
Queued task and attempt become running together in one SQLite transaction;
status events use the existing event repository. A second worker for the same
application database fails immediately while the first holds its OS file lock
(`<app database>.worker.lock`). The file can remain after exit; only the OS lock
indicates ownership, and closing the process releases it. Never delete a live
worker's lock file to start another worker.

At a waiting point the worker returns/persists the proposal or question and
continues serving other queued tasks. Record actual input through InputService,
or approve/reject the exact proposal through ApprovalService. These services
restore running status; the worker finds the recorded receipt, validates it
through Agent.resume, and continues the original persisted thread. No boolean,
text from the model, or proposed action grants permission to write. Exact
receipt retries after completion do not make the task runnable again.

HTTP task creation uses the selected model and Agent configuration versions.
Use the same application configuration/runtime directory for the API and worker.
HTTP `/input` and `/approval` only record human receipts; this independent
worker resumes the original thread. `/cancel` stops queued or human-waiting
tasks; running tasks return a conflict. The worker does not rewrite older tasks.

Execution exceptions mark the task/attempt failed and then propagate to the
operator; they are not retried. Normal completed results remain in checkpoints,
while task/attempt status, events and business ledger stay in the application
database. The files have independent transactions. A crash inside execution,
a running task without a checkpoint, or a non-waiting partial checkpoint needs
explicit recovery and is not automatically rerun. Leases, heartbeat, ownership
epochs, uncertain tool outcomes and crash reconciliation are not provided here.
