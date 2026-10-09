# Agent execution

`agent.state.load_agent_state(database, task_id, owner_id=..., model_version=...)`
builds JSON-compatible state using the owned task's current persisted attempt.
Task, owner, attempt, thread, model and configuration versions are runtime-owned;
none are accepted as tool arguments. A different model version is rejected.
State creation does not start a task or execute business operations.

`agent.graph.Agent(AgentNodes(database, data_dir, rules, model), checkpointer=...)`
executes the same graph for explicit mock, HTTP or local model clients. The
model selects the seven shared tools. Read tools use the real MCP executor;
write calls publish proposals, pause in a separate approval node, and execute
only after the existing ApprovalService has recorded the exact decision.
`start(task_id, owner_id=...)` resolves the saved thread. `resume(task_id,
approval_request_id, owner_id=...)` resumes that thread using the stored receipt;
passing an approval boolean grants no authority. Rejection produces a tool error
without a business write. Transport failures propagate without retry.

For a clarification, the model returns
`{"kind":"ask_user","question":"Please provide the order number."}`.
For a final response, it returns `{"kind":"final","summary":"..."}`.
Tool requests retain their call IDs, and each gets a matching tool message.
Use an explicit `InMemorySaver` only for development; it cannot survive restart.
The graph is not yet connected to the application API, worker or page.
