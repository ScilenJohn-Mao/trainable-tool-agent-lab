import { useState } from 'react';
import { api } from '../api/client';
import type { Proposal } from '../api/types';
import { actionNames, errorMessage, money } from '../format';

export default function ApprovalPanel({ proposal, onSaved }: { proposal: Proposal; onSaved: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [requestId] = useState(() => crypto.randomUUID());
  const [chosen, setChosen] = useState<'approved' | 'rejected' | null>(null);
  const action = proposal.parameters;
  async function decide(decision: 'approved' | 'rejected') {
    setBusy(true); setError(''); setChosen(decision);
    try { await api.approve(proposal.task_id, proposal, decision, requestId); await onSaved(); }
    catch (e) { setError(errorMessage(e)); }
    finally { setBusy(false); }
  }
  return <section className="card approval" aria-labelledby="approval-title">
    <div className="section-heading"><div><span className="eyebrow">需要你的决定</span><h2 id="approval-title">确认处理方案</h2></div><span className="version-tag">提案 v{proposal.proposal_version}</span></div>
    <div className="proposal-action"><div><span className="muted">{actionNames[action.action]}</span><strong>{action.amount_minor === undefined ? '无金额操作' : money(action.amount_minor)}</strong></div><span className="order-tag">{action.order_id || '订单尚未定位'}</span></div>
    {action.amount_minor !== undefined && <p className="muted">CNY · {action.amount_minor} 分</p>}
    <p>{action.summary || action.reason}</p>
    <div className="rule-references">{action.policy_refs.map(ref => <span key={ref.policy_id + ref.version}>{ref.policy_id} · {ref.version}</span>)}</div>
    <p className="muted small">批准绑定此订单、金额和提案版本。确认后才执行；拒绝不会执行此动作。</p>
    {error && <p className="error" role="alert">{error}。可重试同一决定，或刷新核对提案。</p>}
    <div className="button-row"><button className="primary" disabled={busy || chosen === 'rejected'} onClick={() => void decide('approved')}>{busy && chosen === 'approved' ? '正在确认…' : '确认执行'}</button><button className="secondary" disabled={busy || chosen === 'approved'} onClick={() => void decide('rejected')}>{busy && chosen === 'rejected' ? '正在拒绝…' : '拒绝方案'}</button></div>
  </section>;
}
