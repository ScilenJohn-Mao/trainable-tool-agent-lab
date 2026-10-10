import type { PolicyDocument, PolicyReference, Proposal, Task, TaskDetail, TaskEvent } from './types';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, init);
  const body = await response.json().catch(() => { throw new Error('API 服务未返回有效结果，请确认后端已经启动'); });
  if (!response.ok) {
    const code = body.detail?.code;
    const messages: Record<string, string> = {
      order_not_found: '订单不存在或不可访问，请核对订单号',
      task_not_found: '工单不存在或不可访问',
      task_not_cancellable: '当前状态不能取消，请等待处理结果',
      proposal_not_current: '提案版本已变更，请刷新后重新确认',
      proposal_expired: '提案已过期，请刷新核对',
      task_not_waiting_approval: '工单已不处于确认等待状态，请刷新核对',
      task_not_waiting_input: '工单已不处于补充信息状态，请刷新核对',
      input_request_not_current: '补充信息等待点已变更，请刷新核对',
      policy_not_found: '对应版本或条款不存在',
    };
    throw new Error(messages[code] || code || `请求失败 (${response.status})`);
  }
  return body as T;
}
const post = (body?: unknown): RequestInit => ({
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
const taskPath = (id: string) => `/tasks/${encodeURIComponent(id)}`;

export const api = {
  list: () => request<Task[]>('/tasks?limit=100'),
  create: (user_message: string, order_id: string) => request<Task>('/tasks', post({ user_message, ...(order_id ? { order_id } : {}) })),
  detail: (id: string) => request<TaskDetail>(taskPath(id)),
  proposal: (id: string) => request<Proposal | null>(`${taskPath(id)}/proposal`),
  input: (id: string, request_id: string, input_request_id: string, message: string) =>
    request(`${taskPath(id)}/input`, post({ request_id, input_request_id, message })),
  approve: (id: string, proposal: Proposal, decision: 'approved' | 'rejected', request_id: string) =>
    request(`${taskPath(id)}/approval`, post({ request_id, proposal_id: proposal.proposal_id, proposal_version: proposal.proposal_version, decision })),
  cancel: (id: string) => request<Task>(`${taskPath(id)}/cancel`, post()),
  policy: (ref: PolicyReference) => request<PolicyDocument>(
    `/policies/${encodeURIComponent(ref.policy_id)}?${new URLSearchParams({ version: ref.version, section: ref.section })}`,
  ),
};

export async function streamEvents(id: string, signal: AbortSignal, onEvent: (event: TaskEvent) => void) {
  const response = await fetch(`/api${taskPath(id)}/events`, { signal });
  if (!response.ok || !response.body) throw new Error(`进展连接失败 (${response.status})`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
      let boundary;
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = frame.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
        if (data) onEvent(JSON.parse(data) as TaskEvent);
      }
    }
  } finally { reader.releaseLock(); }
}
