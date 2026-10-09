# Agent execution

`agent.state.load_agent_state(database, task_id, owner_id=..., model_version=...)`
builds JSON-compatible state using the owned task's current persisted attempt.
Task, owner, attempt, thread, model and configuration versions are runtime-owned;
none are accepted as tool arguments. A different model version is rejected.
State creation does not start a task or execute business operations.
