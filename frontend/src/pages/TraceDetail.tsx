import {
  AlertTriangle,
  ArrowLeft,
  BookOpenText,
  Bot,
  CheckCircle2,
  ChevronDown,
  Database,
  ExternalLink,
  FileCheck2,
  Fingerprint,
  GitBranch,
  ShieldCheck,
  Wrench,
  XCircle,
} from 'lucide-react'
import { Link, useParams } from 'react-router'

import { EmptyState, LoadingPanel, PageHeading } from '../components/Common'
import { useAgentTrace } from '../lib/api'
import { formatDateTime } from '../lib/format'
import type { AgentTrace, AgentTraceReference, AgentTraceSpan } from '../lib/types'

function duration(value: number | null) {
  if (value === null) return '运行中'
  if (value < 1_000) return `${Math.round(value)} ms`
  return `${(value / 1_000).toFixed(2)} s`
}

function shortHash(value: string | null) {
  return value ? `${value.slice(0, 12)}…${value.slice(-6)}` : '未记录'
}

function SpanIcon({ span }: { span: AgentTraceSpan }) {
  if (span.status === 'failed') return <XCircle size={16} />
  if (span.span_kind === 'agent' || span.span_kind === 'llm' || span.span_kind === 'external') return <Bot size={16} />
  if (span.span_kind === 'validator') return <ShieldCheck size={16} />
  if (span.span_kind === 'persistence') return <Database size={16} />
  return <CheckCircle2 size={16} />
}

function wikiLink(reference: AgentTraceReference) {
  const search = new URLSearchParams({ entry: reference.wiki_entry_id, section: reference.section })
  return { pathname: '/wiki', search: search.toString() }
}

function auditEvidenceLink(trace: AgentTrace, evidenceId: string) {
  if (!trace.audit_url) return undefined
  const separator = trace.audit_url.includes('?') ? '&' : '?'
  return `${trace.audit_url}${separator}evidence=${encodeURIComponent(evidenceId)}`
}

function TraceReferences({ trace, references }: { trace: AgentTrace; references: AgentTraceReference[] }) {
  if (!references.length) {
    return <div className="trace-reference-empty">该节点没有直接引用；它处理的是封签、验证或持久化回执。</div>
  }
  return (
    <div className="trace-reference-list">
      {references.map((reference) => {
        const key = [reference.wiki_entry_id, reference.wiki_version, reference.section, reference.evidence_item_id].join(':')
        const evidenceAuditUrl = reference.evidence_item_id
          ? auditEvidenceLink(trace, reference.evidence_item_id)
          : undefined
        return (
          <article className="trace-reference" key={key}>
            <Link className="trace-reference-wiki" to={wikiLink(reference)}>
              <BookOpenText size={15} />
              <span>
                <strong>{reference.wiki_title}</strong>
                <small>{reference.wiki_entry_id}@{reference.wiki_version} · /{reference.section}</small>
              </span>
              <ExternalLink size={13} />
            </Link>
            {reference.evidence_item_id && (
              reference.source_url ? (
                <a className="trace-reference-evidence" href={reference.source_url} target="_blank" rel="noreferrer">
                  <FileCheck2 size={14} /><span>{reference.evidence_item_id}</span><ExternalLink size={12} />
                </a>
              ) : evidenceAuditUrl ? (
                <Link className="trace-reference-evidence" to={evidenceAuditUrl}>
                  <FileCheck2 size={14} /><span>{reference.evidence_item_id}</span>
                </Link>
              ) : (
                <span className="trace-reference-evidence"><FileCheck2 size={14} />{reference.evidence_item_id}</span>
              )
            )}
            <code title={reference.content_hash}>{shortHash(reference.content_hash)}</code>
          </article>
        )
      })}
    </div>
  )
}

function SpanDetails({ trace, span }: { trace: AgentTrace; span: AgentTraceSpan }) {
  const references = span.references ?? []
  return (
    <div className="trace-span-detail">
      <div className="trace-node-receipt">
        <section>
          <span>输入摘要</span>
          <p>{span.input_summary ?? '该历史节点只保留了输入哈希，没有保存输入内容。'}</p>
          <code title={span.input_digest ?? undefined}><Fingerprint size={12} />{shortHash(span.input_digest)}</code>
        </section>
        <section>
          <span>输出摘要</span>
          <p>{span.output_summary ?? span.summary ?? '该节点没有提供额外输出摘要。'}</p>
          <code title={span.output_digest ?? undefined}><Fingerprint size={12} />{shortHash(span.output_digest)}</code>
        </section>
      </div>
      <div className="trace-node-references">
        <div className="trace-node-detail-title">
          <span>引用血缘</span>
          <small>{references.length} 条 Wiki / Evidence 绑定</small>
        </div>
        <TraceReferences trace={trace} references={references} />
      </div>
      {span.error_summary && (
        <div className="trace-node-error">
          <XCircle size={15} />
          <div><strong>{span.error_code ?? 'Execution error'}</strong><p>{span.error_summary}</p></div>
        </div>
      )}
    </div>
  )
}

export function TraceDetail() {
  const { traceId } = useParams()
  const query = useAgentTrace(traceId)
  if (query.isLoading) return <LoadingPanel />
  if (query.isError || !query.data) {
    return (
      <div className="page trace-detail-page">
        <PageHeading title="Trace 不可用" description={query.error?.message ?? '没有找到该 trace。'} />
        <Link className="secondary-button" to="/observability"><ArrowLeft size={14} />返回运行监控</Link>
      </div>
    )
  }
  const trace = query.data
  const startMs = new Date(trace.started_at).getTime()
  const endMs = trace.completed_at
    ? new Date(trace.completed_at).getTime()
    : startMs + (trace.duration_ms ?? 1)
  const totalMs = Math.max(1, endMs - startMs)
  return (
    <div className="page trace-detail-page">
      <PageHeading
        eyebrow="Trace Inspector"
        title={`${trace.workflow_kind} · ${trace.subject_id.slice(0, 12)}`}
        description="沿执行链查看 Agent 引用、确定性工具收据和脱敏输入输出哈希；完整内容仍由正式审计包负责。"
        actions={(
          <div className="trace-heading-actions">
            <Link className="secondary-button" to="/observability"><ArrowLeft size={14} />返回监控</Link>
            {trace.audit_url && (
              <Link className="primary-button" to={trace.audit_url}>
                <ShieldCheck size={14} />{trace.audit_label ?? '查看业务审计详情'}
              </Link>
            )}
            {trace.external_url && <a className="secondary-button" href={trace.external_url} target="_blank" rel="noreferrer">外部 Trace<ExternalLink size={14} /></a>}
          </div>
        )}
      />

      {!trace.telemetry_complete && (
        <div className="trace-degraded-note"><AlertTriangle size={17} /><div><strong>遥测记录不完整</strong><span>{String(trace.attributes.telemetry_note ?? '部分 span 没有成功持久化或导出。')}</span></div></div>
      )}

      <section className="trace-summary-strip">
        <div><span>Trace ID{trace.attempt_number ? ` · attempt ${trace.attempt_number}` : ''}</span><code>{trace.id}</code></div>
        <div><span>状态</span><strong className={`trace-detail-status ${trace.status}`}>{trace.status}</strong></div>
        <div><span>开始</span><strong>{formatDateTime(trace.started_at, true)}</strong></div>
        <div><span>总耗时</span><strong>{duration(trace.duration_ms)}</strong></div>
        <div><span>节点</span><strong>{trace.span_count}</strong></div>
        <div><span>Policy</span><strong>v{trace.trace_policy_version}</strong></div>
      </section>

      <section className="trace-detail-grid">
        <div className="panel trace-timeline-panel">
          <div className="panel-heading"><div><span className="eyebrow">审计脊柱</span><h2>Agent 与确定性工具节点</h2></div><GitBranch size={19} /></div>
          <div className="trace-timeline-scale"><span>0</span><span>{duration(totalMs / 2)}</span><span>{duration(totalMs)}</span></div>
          <div className="trace-span-list">
            {trace.spans.length === 0 && <EmptyState title="暂无 span" description="根 trace 已建立，但没有可展示的节点收据。" />}
            {trace.spans.map((span) => {
              const references = span.references ?? []
              const spanStart = new Date(span.started_at).getTime()
              const offset = Math.max(0, Math.min(100, ((spanStart - startMs) / totalMs) * 100))
              const width = Math.max(1.5, Math.min(100 - offset, ((span.duration_ms ?? 1) / totalMs) * 100))
              const wikiReferenceCount = new Set(references.map((item) => `${item.wiki_entry_id}:${item.section}`)).size
              const evidenceReferenceCount = new Set(references.map((item) => item.evidence_item_id).filter(Boolean)).size
              return (
                <details className={`trace-span ${span.status}`} key={span.span_id}>
                  <summary>
                    <div className="trace-span-icon"><SpanIcon span={span} /></div>
                    <div className="trace-span-copy"><strong>{span.name}</strong><span>{span.node_id} · {span.span_kind}</span></div>
                    <div className="trace-span-model"><span>{span.agent_id ?? 'deterministic'}</span><strong>{span.model_name ?? span.tool_name ?? 'local validator'}</strong></div>
                    <div className="trace-span-duration">{duration(span.duration_ms)}</div>
                    <div className="trace-span-track"><i style={{ marginLeft: `${offset}%`, width: `${width}%` }} /></div>
                    <div className="trace-span-receipt-counts">
                      {span.tool_name && <span><Wrench size={12} />{span.tool_name}</span>}
                      {wikiReferenceCount > 0 && <span><BookOpenText size={12} />Wiki {wikiReferenceCount}</span>}
                      {evidenceReferenceCount > 0 && <span><FileCheck2 size={12} />证据 {evidenceReferenceCount}</span>}
                    </div>
                    <ChevronDown className="trace-span-chevron" size={16} />
                  </summary>
                  <SpanDetails trace={trace} span={span} />
                </details>
              )
            })}
          </div>
        </div>

        <aside className="panel trace-metadata-panel">
          <div className="panel-heading"><div><span className="eyebrow">安全元数据</span><h2>审计关联</h2></div></div>
          <dl>
            <div><dt>对象 ID</dt><dd><code>{trace.subject_id}</code></dd></div>
            <div><dt>输入封签</dt><dd><code>{trace.input_hash ?? '未记录'}</code></dd></div>
            <div><dt>运行模式</dt><dd>{trace.mode}</dd></div>
            <div><dt>遥测完整</dt><dd>{trace.telemetry_complete ? '是' : '否'}</dd></div>
            {trace.target_id && <div><dt>预测目标</dt><dd><code>{trace.target_id}</code></dd></div>}
            {trace.agent_id && <div><dt>Agent</dt><dd><code>{trace.agent_id}</code></dd></div>}
            {trace.natural_horizon && <div><dt>自然周期</dt><dd>{trace.natural_horizon}</dd></div>}
            {trace.sealed_at && <div><dt>Trace 封签</dt><dd>{formatDateTime(trace.sealed_at, true)}</dd></div>}
            {Object.entries(trace.attributes).map(([key, value]) => (
              <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>
            ))}
          </dl>
          {(trace.artifact_links?.length ?? 0) > 0 && (
            <div className="trace-artifact-links">
              <span>关联审计产物</span>
              {trace.artifact_links?.map((item) => (
                <div key={item.id}><strong>{item.artifact_kind} · {item.relation}</strong><code>{item.artifact_id}</code></div>
              ))}
            </div>
          )}
          {trace.error_summary && <div className="trace-error-box"><strong>{trace.error_code ?? 'Execution error'}</strong><p>{trace.error_summary}</p></div>}
          <div className="trace-policy-box"><ShieldCheck size={16} /><p><strong>Trace 不保存完整 prompt</strong><span>节点只记录脱敏摘要、工具名称、耗时、状态和输入输出哈希；敏感参数及完整正文不会进入 Trace。</span></p></div>
        </aside>
      </section>
    </div>
  )
}
