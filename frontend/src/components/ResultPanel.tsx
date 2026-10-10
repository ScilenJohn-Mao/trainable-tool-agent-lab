import { useState } from 'react';
import { api } from '../api/client';
import type { AgentResult, PolicyDocument, PolicyReference } from '../api/types';
import { actionNames, dateTime, errorMessage, money } from '../format';

function Citation({ reference }: { reference: PolicyReference }) {
  const [document, setDocument] = useState<PolicyDocument | null>(null);
  const [error, setError] = useState('');
  const [open, setOpen] = useState(false);
  async function toggle() {
    setOpen(!open);
    if (!document && !open) {
      try { setDocument(await api.policy(reference)); setError(''); }
      catch (e) { setError(errorMessage(e)); }
    }
  }
  return <div className="citation"><button className="citation-toggle" onClick={() => void toggle()} aria-expanded={open}><span>{reference.policy_id}<small>{reference.version} · {reference.section}</small></span><span>{open ? '−' : '+'}</span></button>{open && <div className="citation-body">{error ? <p className="error" role="alert">{error}</p> : document ? <><strong>{document.title}</strong><p className="policy-text">{document.text}</p><p className="muted small">有效期：{dateTime(reference.effective_from)} 至 {dateTime(reference.effective_to)}（不含终点）</p></> : <p className="muted">正在读取原文…</p>}</div>}</div>;
}
export default function ResultPanel({ result }: { result: AgentResult }) {
  const titles = { resolved: '处理已完成', answered: '已完成事实核对', rejected: '方案已拒绝', failed: '执行未完成' };
  return <section className="card result" aria-labelledby="result-title">
    <div className="section-heading"><h2 id="result-title">{titles[result.outcome]}</h2><span className="version-tag">业务回执</span></div>
    {result.failure_reason && <p className="error" role="alert">{result.failure_reason}</p>}
    {!result.operations.length && <p>本工单没有新增已提交的业务操作。</p>}
    {result.operations.map(op => <div className="operation" key={op.operation_key}><div className="operation-heading"><strong>{actionNames[op.action]} · 已提交</strong><strong>{op.amount_minor === null ? '无金额' : money(op.amount_minor)}</strong></div><p className="muted">{op.order_id || '订单未定位'}{op.amount_minor !== null && ` · ${op.amount_minor} 分 CNY`}</p><dl className="metadata"><dt>操作键</dt><dd>{op.operation_key}</dd><dt>提交时间</dt><dd>{op.committed_at && dateTime(op.committed_at)}</dd></dl></div>)}
    {!!result.policy_citations.length && <><h3>政策依据</h3><p className="muted small">点击引用回读对应版本原文，历史条款须结合有效期判断。</p>{result.policy_citations.map(ref => <Citation key={ref.policy_id + ref.version + ref.section} reference={ref} />)}</>}
    {!!result.rule_refs.length && <details><summary>业务规则引用</summary>{result.rule_refs.map(ref => <p className="muted small" key={ref.policy_id + ref.version}>{ref.policy_id} · {ref.version} · {ref.section}</p>)}</details>}
    <details><summary>订单与核对事实</summary><pre>{JSON.stringify(result.facts, null, 2)}</pre></details>
  </section>;
}
