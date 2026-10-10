import { useEffect, useState } from 'react';
import { api, streamEvents } from '../api/client';
import type { Proposal, Task, TaskDetail, TaskEvent } from '../api/types';
import ApprovalPanel from '../components/ApprovalPanel';
import EventTimeline from '../components/EventTimeline';
import ResultPanel from '../components/ResultPanel';
import { dateTime, errorMessage, statusNames, terminal } from '../format';

function InputPanel({ task, onSaved }: { task: TaskDetail; onSaved: () => Promise<void> }) {
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [requestId] = useState(() => crypto.randomUUID());
  const [submitted, setSubmitted] = useState<string | null>(null);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const reply = submitted ?? message.trim();
    setSubmitted(reply); setBusy(true); setError('');
    try { await api.input(task.task_id, requestId, task.input_request!.request_id, reply); await onSaved(); }
    catch (e) { setError(errorMessage(e)); }
    finally { setBusy(false); }
  }
  return <section className="card input-panel"><span className="eyebrow">需要更多信息</span><h2>补充订单信息</h2><p>{task.input_request!.question}</p><form onSubmit={event => void submit(event)}><label htmlFor="reply">你的回复</label><textarea id="reply" value={submitted ?? message} disabled={submitted !== null} onChange={event => setMessage(event.target.value)} maxLength={4000} placeholder="例如：ORD-1001" required /><button className="primary" disabled={busy || !(submitted ?? message).trim()}>{busy ? '正在提交…' : submitted ? '重试补充信息' : '提交补充信息'}</button></form>{error && <p className="error" role="alert">{error}。请重试同一回复，或刷新核对。</p>}</section>;
}

export default function Workbench() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(new URLSearchParams(location.search).get('task'));
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [connection, setConnection] = useState('等待连接');
  const [refreshKey, setRefreshKey] = useState(0);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [orderId, setOrderId] = useState('');
  const [creating, setCreating] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [filter, setFilter] = useState('all');

  useEffect(() => {
    let active = true;
    async function loadList() {
      try {
        const values = await api.list();
        if (active) { setTasks(values); setSelectedId(current => current ?? values[0]?.task_id ?? null); }
      } catch (e) { if (active) setError(errorMessage(e)); }
    }
    void loadList();
    const timer = setInterval(() => void loadList(), 3000);
    return () => { active = false; clearInterval(timer); };
  }, [refreshKey]);

  useEffect(() => {
    setDetail(null); setProposal(null); setEvents([]);
    if (!selectedId) return;
    history.replaceState(null, '', `?task=${encodeURIComponent(selectedId)}`);
    let active = true;
    const controller = new AbortController();
    let timer: ReturnType<typeof setInterval>;
    async function loadDetail() {
      try {
        const [value, action] = await Promise.all([api.detail(selectedId!), api.proposal(selectedId!)]);
        if (active) {
          setDetail(value); setProposal(action);
          if (value.status === 'cancelled' || (terminal(value.status) && (value.result || value.attempt.model_version === 'manual'))) clearInterval(timer);
        }
      } catch (e) { if (active) setError(errorMessage(e)); }
    }
    void loadDetail();
    timer = setInterval(() => void loadDetail(), 1500);
    setConnection('实时连接中');
    void streamEvents(selectedId, controller.signal, event => {
      if (active) { setEvents(current => [...current, event]); void loadDetail(); }
    }).then(() => { if (active) setConnection('已接收全部进展'); }).catch(e => {
      if (active && !controller.signal.aborted) setConnection(`连接中断 · ${errorMessage(e)}`);
    });
    return () => { active = false; clearInterval(timer); controller.abort(); };
  }, [selectedId, refreshKey]);

  async function refresh() { setError(''); setRefreshKey(key => key + 1); }
  async function create(event: React.FormEvent) {
    event.preventDefault(); setCreating(true); setError('');
    try {
      const task = await api.create(message.trim(), orderId.trim());
      setSelectedId(task.task_id); setMessage(''); setOrderId(''); await refresh();
    } catch (e) { setError(errorMessage(e)); }
    finally { setCreating(false); }
  }
  async function cancel() {
    if (!detail) return;
    setCancelling(true); setError('');
    try { await api.cancel(detail.task_id); await refresh(); }
    catch (e) { setError(errorMessage(e)); }
    finally { setCancelling(false); }
  }
  const waiting = tasks.filter(t => t.status === 'waiting_input' || t.status === 'waiting_approval').length;
  const visible = tasks.filter(task => filter === 'all' || task.status === filter);
  return <div className="app-shell">
    <header className="topbar"><a className="brand" href="/"><span className="brand-icon">T</span><span>Tool Agent Lab<small>售后工单工作台</small></span></a><span className="environment-label"><i />本机 · 模拟售后业务</span></header>
    <main><div className="page-heading"><div><span className="eyebrow">WORKBENCH / 工单工作台</span><h1>每一笔处理，都有依据。</h1><p className="muted">查清事实，确认方案，再完成售后。</p></div><button className="secondary" onClick={() => void refresh()}>刷新工单</button></div>
      <div className="summary-bar"><div><span className="muted">当前工单</span><strong>{tasks.length}<small>条</small></strong></div><div><span className="muted">等待你的处理</span><strong className="accent">{waiting}<small>条</small></strong></div><div><span className="muted">已完成</span><strong>{tasks.filter(t => t.status === 'completed').length}<small>条</small></strong></div><p>金额与成功状态以<br /><strong>实际业务回执</strong>为准</p></div>
      {error && <div className="error-banner" role="alert">{error}<button className="text-button" onClick={() => void refresh()}>重试刷新</button></div>}
      <div className="workspace"><aside className="sidebar">
        <section className="card create-panel"><div className="section-heading"><h2>新建工单</h2><span className="version-tag">＋</span></div><form onSubmit={event => void create(event)}><label htmlFor="message">售后问题</label><textarea id="message" placeholder="描述遇到的问题，例如商品到货损坏…" value={message} onChange={event => setMessage(event.target.value)} required /><label htmlFor="order">订单号 <span className="muted">（可选）</span></label><input id="order" placeholder="例如 ORD-1001" value={orderId} onChange={event => setOrderId(event.target.value)} /><button className="primary full-width" disabled={creating || !message.trim()}>{creating ? '正在创建…' : '创建工单'}</button></form></section>
        <section className="task-list"><div className="section-heading"><h2>工单列表</h2><select aria-label="筛选工单状态" value={filter} onChange={event => setFilter(event.target.value)}><option value="all">全部状态</option>{Object.entries(statusNames).map(([status, label]) => <option key={status} value={status}>{label}</option>)}</select></div>{!visible.length && <p className="muted empty-list">暂无工单。创建一个售后问题开始使用。</p>}{visible.map(task => <button className={`task-item ${selectedId === task.task_id ? 'selected' : ''}`} key={task.task_id} onClick={() => setSelectedId(task.task_id)}><div><span className={`status status-${task.status}`}>{statusNames[task.status]}</span><small>{task.order_id || '未提供订单号'}</small></div><strong>{task.user_message}</strong><small className="muted">{dateTime(task.created_at)}</small></button>)}</section>
      </aside><div className="detail-column">{detail ? <>
        <section className="card task-overview"><div className="section-heading"><span className={`status status-${detail.status}`}>{statusNames[detail.status]}</span>{['queued', 'waiting_input', 'waiting_approval'].includes(detail.status) && <button className="text-button danger" disabled={cancelling} onClick={() => void cancel()}>{cancelling ? '正在取消…' : '取消工单'}</button>}</div><h2>{detail.user_message}</h2><p className="muted">{detail.order_id || '订单号待补充'}</p><div className="model-tag">{detail.attempt.model_version.startsWith('mock') ? '脚本 mock · 用于流程演示' : '模型'} · {detail.attempt.model_version}</div><details><summary>工单标识</summary><dl className="metadata"><dt>工单</dt><dd>{detail.task_id}</dd><dt>图线程</dt><dd>{detail.attempt.thread_id}</dd><dt>配置</dt><dd>{detail.attempt.config_version}</dd></dl></details></section>
        {detail.status === 'queued' && <div className="notice">工单已排队，等待 worker 领取。请确认独立 worker 已启动。</div>}
        {detail.status === 'running' && <div className="notice"><span className="pulse" />正在查询或处理，请稍候。等待方案时不会执行写操作。</div>}
        {detail.status === 'waiting_input' && detail.input_request && <InputPanel key={detail.input_request.request_id} task={detail} onSaved={refresh} />}
        {detail.status === 'waiting_approval' && proposal && <ApprovalPanel key={`${proposal.proposal_id}:${proposal.proposal_version}`} proposal={proposal} onSaved={refresh} />}
        {detail.result && <ResultPanel result={detail.result} />}
        {detail.status === 'completed' && !detail.result && <div className="notice">{detail.attempt.model_version === 'manual' ? '此工单没有保存 Agent 结果，可查看执行进展。' : '状态已完成，正在读取业务回执…'}</div>}
        {detail.status === 'failed' && !detail.result && <div className="error-banner">执行失败，没有已保存的最终回执。请查看执行进展及 worker 日志。</div>}
        {detail.status === 'cancelled' && <div className="notice">此工单已取消。取消不会撤销此前已经提交的业务。</div>}
      </> : <section className="card empty-detail"><div className="empty-mark">↗</div><h2>{selectedId ? '正在读取工单…' : '从一个售后问题开始'}</h2><p className="muted">创建或选择工单，查看事实、处理方案和结果。</p><div className="steps"><span>01 查清事实</span><span>02 确认方案</span><span>03 核对回执</span></div></section>}</div>
      <div className="activity-column"><EventTimeline events={events} connection={connection} onRefresh={() => void refresh()} /></div></div>
      <footer>使用模拟订单与政策 · 金额单位 CNY 分 · 所有写操作均需人工确认</footer>
    </main>
  </div>;
}
