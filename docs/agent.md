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

Supplemental input uses `InputRequest(request_id=..., input_request_id=...,
message=...)` and `agent.submit_input(task_id, request, owner_id=...)`. Copy
`input_request_id` from the current interrupt. The input service appends the
real reply and restores running status in one database transaction. An exact
retry returns its prior receipt; a changed reply or stale input target fails.
`agent.submit_approval(task_id, ApprovalRequest(...), owner_id=...)` uses the
existing confirmation service. Both methods resume the persisted thread only
after checking the receipt, and already consumed exact retries do not rerun the
model or append another user message. Only recorded input sets the trusted
clarification flag. Input text is limited to 4000 characters for event storage.

Final `AgentResult` contains task/attempt/model identity, outcome, actual ledger
operations, refreshed owned order facts, retrieved policy citations and the
confirmed business rule references. The runtime re-reads committed ledger rows
and consumed confirmations; a proposal or model claim is not an operation.
Displayed monetary summaries come from those rows. The model's draft remains
in the conversation trace, so an invented amount cannot replace the receipt.
Retrieved document citations and authorization rule references are separate.
Invalid conclusion shapes, generation truncation and budget stops produce an
explicit failed result. These results are graph state, not yet an API endpoint.

Model-visible context preserves the original request, latest complete turn,
order amounts, committed operation keys, human decisions, input receipts and
policy versions in a runtime-built facts section. Old turns are removed as whole
blocks; assistant tool calls and all matching results are never split. UTF-8
byte budgets include serialized messages and tool schemas. They are not token
estimates: the local loader additionally checks its real tokenizer budget.
Policy bodies/excerpts may be shortened with an explicit `truncated` marker;
references, amounts and authorization facts are not shortened. Full structured
tool replies remain in graph state. If critical information alone does not fit,
the Agent stops with a failed result instead of silently deleting evidence.

The shared prompt is configs/prompts/after_sales.txt; configs/agents/default.yaml
selects it and configs/budgets.yaml. AgentNodes accepts a loaded AgentConfig.
The saved attempt must use the same config version. Output tokens are capped
on the ModelClient configuration used by actual HTTP/local generation, and
decision/tool-call budgets stop the graph before another call or tool batch.
Context/tool byte caps also come from these budgets. Changing these files
requires a new config version for newly created tasks.
