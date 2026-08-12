import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock3,
  GitBranch,
  Radio,
  RefreshCw,
  Search,
} from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'

import { EmptyState, LoadingPanel, PageHeading } from '../components/Common'
import { useAgentObservability, useAgentTraces } from '../lib/api'
import { formatDateTime, percent } from '../lib/format'
import type { AgentTrace, AgentTraceStatus, AgentWorkflowKind } from '../lib/types'

const statusLabel: Record<AgentTraceStatus, string> = {
  running: '执行中',
  completed: '已完成',
  failed: '失败',
  degraded: '遥测降级',
}

const workflowLabel: Record<AgentWorkflowKind, string> = {
  prediction: '预测投委会',
  reflection: '每日反省',
  agent_eval: '离线评测',
}

function duration(value: number | null) {
  if (value === null) return '—'
  if (value < 1_000) return `${Math.round(value)} ms`
  if (value < 60_000) return `${(value / 1_000).toFixed(1)} s`
  return `${(value / 60_000).toFixed(1)} min`
}

function TraceRow({ trace }: { trace: AgentTrace }) {
  return (
    <Link to={`/traces/${trace.id}`} className="trace-row">
      <span className={`trace-status-mark ${trace.status}`}><i /></span>
      <div className="trace-identity">
        <strong>{workflowLabel[trace.workflow_kind]}</strong>
        <code>{trace.id}</code>
      </div>
      <div><span>对象</span><strong>{trace.subject_id}</strong></div>
      <div><span>开始</span><strong>{formatDateTime(trace.started_at, true)}</strong></div>
      <div><span>耗时</span><strong>{duration(trace.duration_ms)}</strong></div>
      <div><span>节点</span><strong>{trace.span_count}</strong></div>
      <div className={`trace-status-copy ${trace.status}`}><strong>{statusLabel[trace.status]}</strong><small>{trace.telemetry_complete ? '遥测完整' : '存在采集缺口'}</small></div>
      <GitBranch size={16} />
    </Link>
  )
}

export function Observability() {
  const [filters, setFilters] = useState({
    workflow_kind: '',
    target_id: '',
    agent_id: '',
    horizon: '',
    status: '',
    started_from: '',
    started_to: '',
  })
  const summaryQuery = useAgentObservability(24)
  const tracesQuery = useAgentTraces({
    ...filters,
    started_from: filters.started_from ? new Date(filters.started_from).toISOString() : '',
    started_to: filters.started_to ? new Date(filters.started_to).toISOString() : '',
  })
  if (summaryQuery.isLoading || tracesQuery.isLoading) return <LoadingPanel />

  const summary = summaryQuery.data
  const traces = tracesQuery.data?.pages.flatMap((page) => page.items) ?? []
  const error = summaryQuery.error ?? tracesQuery.error
  return (
    <div className="page observability-page">
      <PageHeading
        eyebrow="Agent Observability"
        title="运行健康与 Trace"
        description="本地 trace 是非权威遥测视图：用于定位 Agent、验证器与持久化流程，不改变正式预测与 Reflection 收据。"
        actions={(
          <button className="icon-text-button" onClick={() => Promise.all([summaryQuery.refetch(), tracesQuery.refetch()])}>
            <RefreshCw size={15} />刷新
          </button>
        )}
      />
      {error && <div className="action-message error">监控数据不可用：{error.message}</div>}

      <section className="metric-grid observability-metrics">
        <div className="metric-card featured"><div className="metric-icon"><Radio size={18} /></div><span>24h Trace</span><strong>{summary?.total_traces ?? 0}</strong><small>{summary?.running_traces ?? 0} 个正在执行</small></div>
        <div className="metric-card"><div className="metric-icon"><CheckCircle2 size={18} /></div><span>完成率</span><strong>{summary?.completion_rate === null || summary?.completion_rate === undefined ? '—' : percent(summary.completion_rate)}</strong><small>仅统计已结束运行</small></div>
        <div className="metric-card"><div className="metric-icon"><Clock3 size={18} /></div><span>P95 耗时</span><strong>{duration(summary?.p95_duration_ms ?? null)}</strong><small>跨预测、反省与评测</small></div>
        <div className="metric-card"><div className="metric-icon alert"><AlertTriangle size={18} /></div><span>失败 / 降级</span><strong>{(summary?.failed_traces ?? 0) + (summary?.degraded_traces ?? 0)}</strong><small>失败 {summary?.failed_traces ?? 0} · 降级 {summary?.degraded_traces ?? 0}</small></div>
      </section>

      <section className="observability-split">
        <div className="panel trace-ledger">
          <div className="panel-heading">
            <div><span className="eyebrow">执行账本</span><h2>最近 Trace</h2></div>
            <span className="panel-caption">运行中的 trace 每 3 秒刷新</span>
          </div>
          <form className="trace-filter-bar" onSubmit={(event) => event.preventDefault()} aria-label="筛选 Trace">
            <label><span>工作流</span><select value={filters.workflow_kind} onChange={(event) => setFilters({ ...filters, workflow_kind: event.target.value })}><option value="">全部</option>{Object.entries(workflowLabel).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
            <label><span>目标</span><input value={filters.target_id} onChange={(event) => setFilters({ ...filters, target_id: event.target.value })} placeholder="target_id" /></label>
            <label><span>Agent</span><input value={filters.agent_id} onChange={(event) => setFilters({ ...filters, agent_id: event.target.value })} placeholder="agent_id" /></label>
            <label><span>周期</span><select value={filters.horizon} onChange={(event) => setFilters({ ...filters, horizon: event.target.value })}><option value="">全部</option><option>D1</option><option>W1</option><option>D20</option></select></label>
            <label><span>状态</span><select value={filters.status} onChange={(event) => setFilters({ ...filters, status: event.target.value })}><option value="">全部</option>{Object.entries(statusLabel).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
            <label><span>开始时间（从）</span><input type="datetime-local" value={filters.started_from} onChange={(event) => setFilters({ ...filters, started_from: event.target.value })} /></label>
            <label><span>开始时间（至）</span><input type="datetime-local" value={filters.started_to} onChange={(event) => setFilters({ ...filters, started_to: event.target.value })} /></label>
            <Search size={15} aria-hidden="true" />
          </form>
          <div className="trace-list">
            {traces.length === 0 && <EmptyState title="还没有 Agent trace" description="新预测、Reflection 或离线评测执行后会显示在这里。" />}
            {traces.map((trace) => <TraceRow key={trace.id} trace={trace} />)}
          </div>
          {tracesQuery.hasNextPage && (
            <div className="trace-pagination">
              <button
                type="button"
                className="secondary-button"
                disabled={tracesQuery.isFetchingNextPage}
                onClick={() => tracesQuery.fetchNextPage()}
              >
                {tracesQuery.isFetchingNextPage ? '加载中…' : '加载更多 Trace'}
              </button>
            </div>
          )}
        </div>

        <aside className="panel trace-health-panel">
          <div className="panel-heading"><div><span className="eyebrow">覆盖范围</span><h2>工作流构成</h2></div><Activity size={19} /></div>
          <div className="workflow-kind-list">
            {(Object.keys(workflowLabel) as AgentWorkflowKind[]).map((kind) => (
              <div key={kind}><span>{workflowLabel[kind]}</span><strong>{summary?.by_workflow_kind[kind] ?? 0}</strong></div>
            ))}
          </div>
          <div className="telemetry-completeness">
            <span>遥测完整率</span>
            <strong>{summary?.telemetry_complete_rate === null || summary?.telemetry_complete_rate === undefined ? '—' : percent(summary.telemetry_complete_rate)}</strong>
            <div><i style={{ width: `${(summary?.telemetry_complete_rate ?? 0) * 100}%` }} /></div>
          </div>
          {summary?.database_size_bytes !== null && summary?.database_size_bytes !== undefined && (
            <div className={`trace-storage-health${summary.storage_warning ? ' warning' : ''}`}>
              <span>本地 Trace 存储</span>
              <strong>{(summary.database_size_bytes / 1024 / 1024).toFixed(1)} MB</strong>
              <small>{summary.storage_warning ? '数据库体积达到监控阈值' : `${summary.stored_span_count ?? 0} spans · ${summary.stored_artifact_link_count ?? 0} artifact links`}</small>
            </div>
          )}
          <p>OTLP 导出失败时，本地业务流程继续执行，并在 trace 上标记采集缺口。</p>
        </aside>
      </section>
    </div>
  )
}
