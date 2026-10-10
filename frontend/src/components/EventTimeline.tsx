import type { TaskEvent } from '../api/types';
import { actionNames, dateTime, statusNames } from '../format';

function eventTitle(event: TaskEvent) {
  const p = event.payload;
  if (event.event_type === 'task_status_changed') return statusNames[p.status as keyof typeof statusNames] || String(p.status);
  if (event.event_type === 'tool_call') return actionNames[String(p.tool)] || String(p.tool);
  if (event.event_type === 'tool_result') {
    const result = p.result as { status?: string; error?: { code: string } };
    return result?.status === 'error' ? `工具未完成 · ${result.error?.code}` : `${actionNames[String(p.tool)] || p.tool} · 已返回`;
  }
  return ({ input_requested: '等待补充信息', input_received: '已收到补充信息', action_proposed: '已生成处理方案', approval_recorded: '已记录人工决定' } as Record<string, string>)[event.event_type] || event.event_type;
}
export default function EventTimeline({ events, connection, onRefresh }: { events: TaskEvent[]; connection: string; onRefresh: () => void }) {
  return <section className="card timeline" aria-labelledby="timeline-title">
    <div className="section-heading"><h2 id="timeline-title">执行进展</h2><span className="muted small">{connection}</span></div>
    {connection.startsWith('连接中断') && <button className="text-button" onClick={onRefresh}>重新连接</button>}
    {!events.length && <p className="muted">工单开始处理后，进展会显示在这里。</p>}
    <ol>{events.map(event => <li key={event.seq}>
      <span className="event-dot" /><div><div className="event-topline"><strong>{eventTitle(event)}</strong><span className="muted small">#{event.seq}</span></div><time className="muted small">{dateTime(event.occurred_at)}</time>
        {event.event_type === 'input_requested' && <p>{String(event.payload.question)}</p>}
        {event.event_type === 'approval_recorded' && <p>{String(event.payload.decision) === 'approved' ? '操作者批准' : '操作者拒绝'}</p>}
        {(event.event_type === 'tool_call' || event.event_type === 'tool_result') && <details><summary>查看工具{event.event_type === 'tool_call' ? '参数' : '结果'}</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>}
      </div>
    </li>)}</ol>
  </section>;
}
