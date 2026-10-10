export type TaskStatus = 'queued' | 'running' | 'waiting_input' | 'waiting_approval' | 'completed' | 'failed' | 'cancelled';
export interface Task {
  task_id: string; owner_id: string; user_message: string; order_id: string | null;
  current_attempt_id: string | null; status: TaskStatus; created_at: string;
}
export interface Identity {
  task_id: string; attempt_id: string; thread_id: string; model_version: string; config_version: string;
}
export interface PolicyReference {
  policy_id: string; version: string; section: string; effective_from: string; effective_to: string;
}
export interface Action {
  action: 'request_refund' | 'issue_coupon' | 'create_handoff';
  order_id: string | null; amount_minor?: number; currency?: 'CNY'; reason: string;
  summary?: string; policy_refs: PolicyReference[];
}
export interface Proposal {
  task_id: string; attempt_id: string; proposal_id: string; proposal_version: number;
  parameters: Action; created_at: string; expires_at: string | null;
}
export interface Operation {
  operation_key: string; order_id: string | null; action: Action['action'];
  amount_minor: number | null; currency: 'CNY'; status: 'pending' | 'succeeded' | 'failed'; committed_at: string | null;
}
export interface AgentResult {
  outcome: 'resolved' | 'answered' | 'rejected' | 'failed'; summary: string;
  model_version: string; operations: Operation[]; facts: Record<string, unknown>;
  policy_citations: PolicyReference[]; rule_refs: PolicyReference[]; failure_reason: string | null;
}
export interface TaskDetail extends Task {
  attempt: Identity & { status: TaskStatus };
  input_request: { kind: string; request_id: string; question: string } | null;
  result: AgentResult | null;
}
export interface TaskEvent extends Identity {
  seq: number; event_type: string; occurred_at: string; call_id: string | null; payload: Record<string, unknown>;
}
export interface PolicyDocument {
  reference: PolicyReference; title: string; category: string; text: string;
}
