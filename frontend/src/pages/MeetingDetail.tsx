import { AlertOctagon, Check, ChevronDown, CircleDot, Clock3, Scale, SearchCheck, ShieldAlert, UsersRound } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router'

import { CitationList } from '../components/Citations'
import { DemoBanner, DirectionBadge, EmptyState, LoadingPanel, PageHeading, QualityBadge, StatusBadge } from '../components/Common'
import { ForecastTargetSelector } from '../components/ForecastTargetSelector'
import { ProbabilityBar } from '../components/Probability'
import { useLatestForecasts, useMeeting } from '../lib/api'
import { formatDateTime, percent } from '../lib/format'
import type { AgentOpinion, Horizon } from '../lib/types'

function OpinionCard({ opinion, instrumentCount }: { opinion: AgentOpinion; instrumentCount: number }) {
  const [expanded, setExpanded] = useState(false)
  const isPlaceholder = opinion.status === 'placeholder'

  return (
    <article className={`opinion-card${isPlaceholder ? ' placeholder' : ''}`}>
      <div className="opinion-card-head">
        <div className="agent-identity">
          <div className="agent-avatar large">{isPlaceholder ? 'Q' : opinion.agent_name.slice(0, 1)}</div>
          <div><h3>{opinion.agent_name}</h3><span>{opinion.role}</span></div>
        </div>
        <div className="opinion-head-right">
          {isPlaceholder && <span className="placeholder-tag">占位 · 权重 0</span>}
          <DirectionBadge direction={opinion.direction} />
        </div>
      </div>
      <ProbabilityBar probabilities={opinion.probabilities} compact />
      {opinion.strategy_context && (
        <div className="strategy-context" aria-label="策略配置上下文">
          <span>市场状态<strong>{{ risk_on: '风险偏好', balanced: '均衡', risk_off: '避险' }[opinion.strategy_context.market_regime]}</strong></span>
          <span>风格<strong>{{ large_cap: '大盘', mid_small_cap: '中小盘', growth: '成长', balanced: '均衡' }[opinion.strategy_context.style_bias]}</strong></span>
          <span>标的排序<strong>{opinion.strategy_context.rank_tied ? '并列' : ''}#{opinion.strategy_context.relative_rank}/{instrumentCount}</strong></span>
          <span>配置分数<strong>{opinion.strategy_context.allocation_score >= 0 ? '+' : ''}{opinion.strategy_context.allocation_score.toFixed(3)}</strong></span>
        </div>
      )}
      <p className="opinion-summary">{opinion.summary}</p>
      {!isPlaceholder && (
        <>
          <div className="evidence-columns">
            <div>
              <h4><Check size={15} /> 支持证据</h4>
              <ul>{opinion.evidence.map((item) => <li key={item}>{item}</li>)}</ul>
            </div>
            <div className="counter">
              <h4><ShieldAlert size={15} /> 反方证据</h4>
              <ul>{opinion.counter_evidence.map((item) => <li key={item}>{item}</li>)}</ul>
            </div>
          </div>
          <button className="expand-button" onClick={() => setExpanded((value) => !value)}>
            {expanded ? '收起验证材料' : `查看 ${opinion.citations.length} 条引用与失效条件`}
            <ChevronDown className={expanded ? 'rotated' : ''} size={16} />
          </button>
          {expanded && (
            <div className="opinion-expanded">
              <div>
                <h4>可证伪条件</h4>
                <ol>{opinion.invalidation_conditions.map((item) => <li key={item}>{item}</li>)}</ol>
              </div>
              <CitationList citations={opinion.citations} />
            </div>
          )}
        </>
      )}
    </article>
  )
}

export function MeetingDetail() {
  const { runId: routeRunId } = useParams()
  const latest = useLatestForecasts(!routeRunId)
  const runId = routeRunId ?? latest.data?.data.run_id
  const query = useMeeting(runId, {
    allowDemoFallback: !routeRunId && latest.data?.demo_reason === 'fallback',
  })
  const [indexCode, setIndexCode] = useState<string>('')
  const [horizon, setHorizon] = useState<Horizon>('D2')
  const meeting = query.data?.data
  const instruments = useMemo(
    () => [...new Map(
      meeting?.forecasts.map((forecast) => [
        forecast.index_code,
        { code: forecast.index_code, name: forecast.index_name },
      ]) ?? [],
    ).values()],
    [meeting],
  )
  const selectedIndexCode = instruments.some((item) => item.code === indexCode)
    ? indexCode
    : instruments[0]?.code ?? indexCode

  const finalForecast = meeting?.forecasts.find(
    (forecast) => forecast.index_code === selectedIndexCode && forecast.horizon === horizon,
  )
  const opinions = useMemo(() => {
    if (!meeting) return []
    return meeting.opinions.filter((item) => item.index_code === selectedIndexCode && item.horizon === horizon)
  }, [meeting, selectedIndexCode, horizon])
  const activeAgentCount = useMemo(
    () => new Set(meeting?.opinions.filter((item) => item.status === 'active').map((item) => item.agent_id)).size,
    [meeting],
  )

  if ((!routeRunId && latest.isLoading) || query.isLoading) return <LoadingPanel />
  if (!runId) return <EmptyState title="没有最新运行" description="创建并完成一次投委会运行后再查看。" />
  if (query.isError) {
    return (
      <div className="page meeting-page">
        <PageHeading
          eyebrow="决策档案"
          title="投委会决策详情"
          description={`运行 ${runId} 的会议数据加载失败，未使用其他运行或静态 Demo 意见替代。`}
        />
        <div className="action-message error" role="alert">{query.error.message}</div>
        <EmptyState title="会议记录不可用" description="请在运行页确认该 run 的状态，或稍后重试。" />
      </div>
    )
  }
  if (!meeting) return <EmptyState title="没有会议记录" description={`运行 ${runId} 尚未返回会议数据。`} />

  return (
    <div className="page meeting-page">
      <PageHeading
        eyebrow="决策档案"
        title="投委会决策详情"
        description="逐步查看每个 Agent 的观点、分歧、反证与最终汇总。"
        actions={
          <div className="meeting-heading-actions">
            <Link className="secondary-button" to={`/reflections?run=${encodeURIComponent(meeting.run.id)}`}>
              <SearchCheck size={15} /> 查看到期反省
            </Link>
            <div className="meeting-meta">
              <StatusBadge status={meeting.run.status} />
              <span>{meeting.run.id}</span>
            </div>
          </div>
        }
      />
      {query.data?.mode === 'demo' && <DemoBanner error={query.data.error} reason={query.data.demo_reason} />}

      <section className="meeting-context panel">
        <div className="context-item"><Clock3 size={17} /><div><span>决策时间</span><strong>{formatDateTime(meeting.run.as_of)}</strong></div></div>
        <div className="context-item"><CircleDot size={17} /><div><span>数据截止</span><strong>{formatDateTime(meeting.run.data_cutoff)}</strong></div></div>
        <div className="context-item"><UsersRound size={17} /><div><span>有效意见</span><strong>{activeAgentCount} 个 Agent</strong></div></div>
        <div className="context-item"><Scale size={17} /><div><span>数据质量</span><QualityBadge quality={meeting.run.data_quality} /></div></div>
      </section>

      <ForecastTargetSelector
        instruments={instruments}
        indexCode={selectedIndexCode}
        horizon={horizon}
        onIndexChange={setIndexCode}
        onHorizonChange={setHorizon}
      />

      {finalForecast && (
        <section className="final-decision panel">
          <div className="final-decision-label"><span>CIO 最终决策</span><DirectionBadge direction={finalForecast.direction} /></div>
          <div className="final-decision-body">
            <div className="final-title">
              <span>{finalForecast.index_name} · {horizon}</span>
              <strong>{percent(finalForecast.confidence)}</strong>
              <small>排除小波动后的方向置信度</small>
            </div>
            <div className="final-probability"><ProbabilityBar probabilities={finalForecast.probabilities} /></div>
            <p>{finalForecast.rationale}</p>
          </div>
          <div className="final-decision-foot">
            <span>评价噪声带：±{percent(finalForecast.threshold, 2)}</span>
            {finalForecast.target_date && <span>目标交易日：{finalForecast.target_date}</span>}
            <span>{finalForecast.citations.length} 条引用已冻结</span>
          </div>
        </section>
      )}

      <div className="meeting-body-grid">
        <section>
          <div className="section-heading compact-heading">
            <div><span className="eyebrow">委员意见</span><h2>委员观点与分歧</h2></div>
            <span className="subtle-text">按研究节点原始输出展示</span>
          </div>
          <div className="opinion-stack">
            {opinions.length
              ? opinions.map((opinion) => (
                  <OpinionCard
                    key={opinion.id}
                    opinion={opinion}
                    instrumentCount={instruments.length}
                  />
                ))
              : <EmptyState title="该标的暂无委员意见" description={`运行 ${meeting.run.id} 未返回 ${selectedIndexCode} · ${horizon} 的结构化意见。`} />}
          </div>
        </section>

        <aside className="meeting-rail">
          <div className="panel workflow-panel">
            <div className="panel-heading"><div><span className="eyebrow">处理轨迹</span><h2>决策轨迹</h2></div></div>
            <div className="workflow-list">
              {meeting.workflow_steps.map((step, index) => (
                <div className="workflow-step" key={step.id}>
                  <div className="workflow-marker"><Check size={13} />{index < meeting.workflow_steps.length - 1 && <i />}</div>
                  <div><strong>{step.label}</strong><span>{step.detail}</span></div>
                </div>
              ))}
            </div>
          </div>

          <div className="panel critic-panel">
            <div className="critic-icon"><AlertOctagon size={19} /></div>
            <div><span className="eyebrow">风险反方</span><h3>主要反证</h3></div>
            {(() => {
              const critic = opinions.find((opinion) => opinion.agent_id === 'risk_critic_agent')
              return critic ? (
                <>
                  <p>{critic.summary}</p>
                  <div className="critic-result"><ShieldAlert size={15} /><span>{critic.contribution}</span></div>
                </>
              ) : (
                <p>本次会议没有返回 Risk Critic 结构化意见。</p>
              )
            })()}
          </div>
        </aside>
      </div>
    </div>
  )
}
