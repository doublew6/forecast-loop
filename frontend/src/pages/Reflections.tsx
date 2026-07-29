import {
  AlertTriangle,
  ArrowRight,
  BrainCircuit,
  CheckCircle2,
  Clock3,
  ExternalLink,
  GitCompareArrows,
  Lightbulb,
  SearchCheck,
  ShieldQuestion,
  Target,
  XCircle,
} from 'lucide-react'
import { useMemo } from 'react'
import { Link, useSearchParams } from 'react-router'

import {
  DemoBanner,
  DirectionBadge,
  EmptyState,
  LoadingPanel,
  PageHeading,
} from '../components/Common'
import { useLessons, useMeeting, useReflection, useReflections } from '../lib/api'
import { formatDateTime, percent, signedPercent } from '../lib/format'
import { selectRelatedLessons } from '../lib/reflections'
import type {
  AvailabilityClass,
  CausalLevel,
  LessonProposal,
  LessonStatus,
  ReflectionFinding,
  ReflectionSource,
  ReflectionSeverity,
  ReflectionStatus,
} from '../lib/types'
import { INDEXES } from '../lib/types'

const severityLabel: Record<ReflectionSeverity, string> = {
  noise: '噪声带内',
  directional: '方向性波动',
  large: '显著波动',
  extreme: '极端冲击',
  unknown: '待判定',
}

const severityDescription: Record<ReflectionSeverity, string> = {
  noise: '实际收益落在预测时冻结的评价噪声带内。',
  directional: '结果已越过噪声带，但没有触发极端行情门槛。',
  large: '标准化收益或历史分位显示本次波动显著。',
  extreme: '预设严重度规则确认尾部冲击，不按普通噪声解释。',
  unknown: '结果诊断尚未形成，不能提前归类为噪声。',
}

const availabilityLabel: Record<AvailabilityClass, string> = {
  available_used: '截止前已使用',
  available_missed: '截止前遗漏',
  coverage_gap_pre_cutoff: '截止前覆盖缺口',
  post_cutoff_event: '截止后新事件',
  after_close_explanation: '收盘后解释',
  unresolved: '时间归属未决',
}

const causalLabel: Record<CausalLevel, string> = {
  verified: '因果已验证',
  supported: '有证据支持',
  hypothesis: '待验证假设',
  unresolved: '因果未决',
}

const sourceTimeLabel: Record<ReflectionSource['time_class'], string> = {
  published_before_cutoff_not_frozen: '截止前公开 · 原系统未冻结',
  post_cutoff_preclose: '截止后 · 收盘前',
  post_close_explanation: '收盘后解释',
  unresolved: '发布时间未决',
}

const counterfactualBasisLabel: Record<string, string> = {
  original_frozen_evidence: '仅使用原冻结证据',
  post_cutoff_oracle: '加入截止后信息的事后敏感度',
  leave_one_input_out: '移除单项输入',
  not_applicable: '不适用',
}

function contributionLabel(item: Record<string, unknown>): string {
  const name = item.name
    ?? item.sector
    ?? item.industry
    ?? item.index_name
    ?? item.code
    ?? item.symbol
  const value = item.contribution
    ?? item.return_contribution
    ?? item.weighted_return
    ?? item.value
  if (name === undefined && value === undefined) return JSON.stringify(item)
  const numericValue = typeof value === 'number'
    ? `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
    : value
  return [name, numericValue].filter((part) => part !== undefined).join(' ')
}

const lessonStatusLabel: Record<LessonStatus, string> = {
  candidate: '候选经验',
  active: '经验已激活',
  proposed: '待晋升',
  promoted: '已晋升',
  challenged: '受反证挑战',
  retired: '已退役',
  superseded: '已被替代',
}

const lessonBlockerLabel: Record<string, string> = {
  shadow_target_dates_below_minimum: '影子运行尚未达到 20 个独立 Live 日期',
  independent_episodes_below_5: '独立市场事件少于 5 个',
  replay_target_dates_below_20: '重放目标日期少于 20 个',
  average_brier_not_improved: '平均 Brier 尚未改善',
  calibration_not_improved: '概率校准尚未改善',
  important_subgroup_regression_not_cleared: '重要子组不退化尚未确认',
}

const revalidationReasonLabel: Record<string, string> = {
  monthly: '月度复核到期',
  new_20_target_dates: '新增 20 个目标日期',
  half_life_60_sessions: '达到 60 个交易日半衰期',
}

const lessonLifecycleEventLabel: Record<string, string> = {
  replay_recorded: '记录重放',
  approved: '人工激活',
  revalidated: '完成复核',
  challenged: '标记挑战',
  retired: '退役',
  superseded: '被替代',
}

function shortHash(hash: string): string {
  const normalized = hash.replace(/^sha256:/, '')
  return normalized.length > 12 ? `${normalized.slice(0, 12)}…` : normalized
}

const reflectionStatusLabel: Record<ReflectionStatus, string> = {
  awaiting_sources: '等待行情',
  awaiting_analysis: '等待分析',
  completed: '已完成',
  failed: '失败',
  blocked_upstream: '上游阻断',
}

function SeverityBadge({ severity }: { severity: ReflectionSeverity }) {
  return <span className={`severity-badge ${severity}`}>{severityLabel[severity]}</span>
}

function ReflectionStatusBadge({ status }: { status: ReflectionStatus }) {
  return <span className={`reflection-status ${status}`}><i />{reflectionStatusLabel[status]}</span>
}

function FindingCard({
  finding,
  agentName,
}: {
  finding: ReflectionFinding
  agentName?: string
}) {
  const title = agentName
    ?? finding.agent_name
    ?? finding.agent_id
    ?? finding.index_name
    ?? finding.index_code
    ?? (finding.scope_type === 'committee' ? '投委会整体' : '市场结果')
  const hasRightWrong = finding.what_was_right.length > 0 || finding.what_was_wrong.length > 0

  return (
    <article className="reflection-finding-card">
      <header>
        <div className="finding-identity">
          <span className={`finding-scope ${finding.scope_type}`}>
            {finding.scope_type === 'agent' ? 'Agent' : finding.scope_type === 'committee' ? 'Committee' : 'Market event'}
          </span>
          <div>
            <h3>{title}</h3>
            <small>
              {[finding.index_name ?? finding.index_code, finding.horizon].filter(Boolean).join(' · ') || '跨市场归因'}
            </small>
          </div>
        </div>
        <div className="finding-verdict">
          {finding.direction_correct === true
            ? <CheckCircle2 size={16} />
            : finding.direction_correct === false
              ? <XCircle size={16} />
              : <ShieldQuestion size={16} />}
          <strong>{finding.outcome_verdict}</strong>
        </div>
      </header>

      <div className="finding-classification">
        <span className={`availability-badge ${finding.availability_class}`}>
          <Clock3 size={12} />{availabilityLabel[finding.availability_class]}
        </span>
        <span className={`causal-badge ${finding.causal_level}`}>
          <SearchCheck size={12} />{causalLabel[finding.causal_level]}
          {finding.causal_confidence !== undefined && ` · ${percent(finding.causal_confidence)}`}
        </span>
        {finding.error_types.map((type) => <span className="error-type-tag" key={type}>{type}</span>)}
      </div>

      {finding.attribution && <p className="finding-attribution">{finding.attribution}</p>}
      {finding.invalidation_conditions_triggered.length > 0 && (
        <div className="triggered-conditions">
          <strong>已触发失效条件</strong>
          <ul>
            {finding.invalidation_conditions_triggered.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}
      {hasRightWrong && (
        <div className="finding-comparison">
          <div className="right">
            <h4><CheckCircle2 size={14} /> 判断对在哪里</h4>
            {finding.what_was_right.length
              ? <ul>{finding.what_was_right.map((item) => <li key={item}>{item}</li>)}</ul>
              : <p>没有足够证据认定为“对得有理”。</p>}
          </div>
          <div className="wrong">
            <h4><XCircle size={14} /> 判断错在哪里</h4>
            {finding.what_was_wrong.length
              ? <ul>{finding.what_was_wrong.map((item) => <li key={item}>{item}</li>)}</ul>
              : <p>没有确认的过程错误。</p>}
          </div>
        </div>
      )}

      {(
        finding.original_evidence_ids.length > 0
        || finding.missed_evidence_ids.length > 0
        || finding.used_source_ids.length > 0
      ) && (
        <div className="finding-evidence-ledger">
          {finding.original_evidence_ids.length > 0 && (
            <div>
              <strong>原预测已用证据</strong>
              <div>{finding.original_evidence_ids.map((id) => <code key={id}>{id}</code>)}</div>
            </div>
          )}
          {finding.missed_evidence_ids.length > 0 && (
            <div>
              <strong>截止前遗漏证据</strong>
              <div>{finding.missed_evidence_ids.map((id) => <code key={id}>{id}</code>)}</div>
            </div>
          )}
          {finding.used_source_ids.length > 0 && (
            <div>
              <strong>事后冻结来源</strong>
              <div>
                {finding.used_source_ids.map((id) => (
                  <a href={`#reflection-source-${id}`} key={id}>{id}</a>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {(
        finding.counterfactual_direction
        || finding.counterfactual_basis
        || finding.counterfactual_explanation
        || finding.remediation.length > 0
      ) && (
        <footer>
          {(finding.counterfactual_direction || finding.counterfactual_basis) && (
            <div className="counterfactual">
              <GitCompareArrows size={14} />
              <div>
                <span>反事实敏感度</span>
                <small>
                  {counterfactualBasisLabel[finding.counterfactual_basis ?? '']
                    ?? finding.counterfactual_basis
                    ?? '分析口径未标注'}
                </small>
              </div>
              {finding.counterfactual_direction && (
                <DirectionBadge direction={finding.counterfactual_direction} subtle />
              )}
              {finding.would_flip !== undefined && (
                <strong>{finding.would_flip ? '会翻转' : '不翻转'}</strong>
              )}
            </div>
          )}
          {finding.counterfactual_probabilities && (
            <div className="counterfactual-probabilities">
              <span>上涨 {percent(finding.counterfactual_probabilities.up)}</span>
              <span>噪声区 {percent(finding.counterfactual_probabilities.neutral)}</span>
              <span>下跌 {percent(finding.counterfactual_probabilities.down)}</span>
            </div>
          )}
          {finding.counterfactual_explanation && (
            <p className="counterfactual-explanation">{finding.counterfactual_explanation}</p>
          )}
          {finding.remediation.length > 0 && (
            <div className="remediation">
              <strong>改进动作</strong>
              <ul>{finding.remediation.map((item) => <li key={item}>{item}</li>)}</ul>
            </div>
          )}
        </footer>
      )}
    </article>
  )
}

function LessonCard({ lesson }: { lesson: LessonProposal }) {
  const recentHistory = lesson.lifecycle_history.slice(-3).reverse()
  const wikiState = lesson.status === 'promoted'
    ? '已晋升正式 Wiki'
    : '尚未晋升正式 Wiki'
  return (
    <article className="lesson-card">
      <div className="lesson-card-head">
        <span className={`lesson-status ${lesson.status}`}>{lessonStatusLabel[lesson.status]}</span>
        <small>{lesson.independent_episode_count} 个独立 episode</small>
      </div>
      <h3>{lesson.title}</h3>
      <p>{lesson.summary}</p>
      <div className="lesson-evidence">
        <span>支持 <strong>{lesson.support_count}</strong></span>
        <span>反例 <strong>{lesson.counterexample_count}</strong></span>
        <span>重放日期 <strong>{lesson.replay_target_dates}</strong></span>
        <span>重放批次 <strong>{lesson.replay_batch_count}</strong></span>
        <span>半衰期 <strong>{lesson.half_life_sessions} 交易日</strong></span>
      </div>
      <div className="lesson-wiki-state" role="note">
        <strong>{wikiState}</strong>
        <span>
          {lesson.status === 'active'
            ? 'active 只表示经验生命周期已激活，正式 Wiki 仍需独立人工晋升。'
            : '经验生命周期与正式 Wiki 发布状态相互独立。'}
        </span>
      </div>
      <div className="lesson-readiness">
        <span className={`wiki-readiness ${
          lesson.wiki_review_ready === true
            ? 'ready'
            : lesson.wiki_review_ready === false
              ? 'blocked'
              : 'unassessed'
        }`}>
          {lesson.wiki_review_ready === true
            ? 'Wiki 人工审查已就绪'
            : lesson.wiki_review_ready === false
              ? 'Wiki 人工审查未就绪'
              : 'Wiki 审查门槛尚未评估'}
        </span>
        {lesson.replay_blockers.length > 0 && (
          <ul className="lesson-blockers">
            {lesson.replay_blockers.map((blocker) => (
              <li key={blocker}>{lessonBlockerLabel[blocker] ?? blocker}</li>
            ))}
          </ul>
        )}
      </div>
      {lesson.revalidation_due && (
        <div className="lesson-revalidation" role="status">
          <strong>需要重新验证</strong>
          <span>
            {lesson.revalidation_due_reasons.length
              ? lesson.revalidation_due_reasons
                .map((reason) => revalidationReasonLabel[reason] ?? reason)
                .join(' · ')
              : '复核门槛已触发'}
          </span>
        </div>
      )}
      {(lesson.latest_replay_hash || lesson.supersedes_id || lesson.superseded_by_id) && (
        <div className="lesson-ledger">
          {lesson.latest_replay_hash && (
            <span>
              最近重放 hash
              {' '}
              <code title={lesson.latest_replay_hash}>{shortHash(lesson.latest_replay_hash)}</code>
            </span>
          )}
          {lesson.supersedes_id && <span>替代自 <code>{lesson.supersedes_id}</code></span>}
          {lesson.superseded_by_id && <span>已由 <code>{lesson.superseded_by_id}</code> 替代</span>}
        </div>
      )}
      {recentHistory.length > 0 && (
        <div className="lesson-history">
          <strong>最近生命周期事件</strong>
          <ol>
            {recentHistory.map((event) => (
              <li key={event.id}>
                <div>
                  <span>{lessonLifecycleEventLabel[event.event_type] ?? event.event_type}</span>
                  <small>
                    {lessonStatusLabel[event.from_status]} → {lessonStatusLabel[event.to_status]}
                  </small>
                </div>
                <small>
                  {event.occurred_at ? formatDateTime(event.occurred_at) : '时间未记录'}
                  {' · '}
                  {event.actor}
                </small>
                {event.reason && <p title={event.reason}>{event.reason}</p>}
              </li>
            ))}
          </ol>
        </div>
      )}
      <footer>
        <span>{lesson.target_wiki_entry_id ?? '尚未指定 Wiki 条目'}</span>
        {lesson.review_after && <span>复审 {lesson.review_after}</span>}
        {!lesson.review_after && lesson.reviewed_at && <span>已审查 {lesson.reviewed_at.slice(0, 10)}</span>}
      </footer>
    </article>
  )
}

export function Reflections() {
  const [searchParams, setSearchParams] = useSearchParams()
  const listQuery = useReflections()
  const runFilter = searchParams.get('run')
  const agentFilter = searchParams.get('agent')
  const requestedId = searchParams.get('id')
  const reflections = useMemo(() => {
    const rows = listQuery.data?.data ?? []
    return runFilter ? rows.filter((item) => item.source_run_id === runFilter) : rows
  }, [listQuery.data, runFilter])
  const selectedId = requestedId && reflections.some((item) => item.id === requestedId)
    ? requestedId
    : reflections[0]?.id
  const detailQuery = useReflection(selectedId)
  const lessonsQuery = useLessons()
  const detail = detailQuery.data?.data
  const meetingQuery = useMeeting(detail?.source_run_id)
  const meeting = meetingQuery.data?.data

  const agentNames = useMemo(
    () => new Map(meeting?.opinions.map((opinion) => [opinion.agent_id, opinion.agent_name]) ?? []),
    [meeting],
  )
  const findings = useMemo(() => {
    if (!detail) return []
    return agentFilter
      ? detail.findings.filter((finding) => finding.agent_id === agentFilter)
      : detail.findings
  }, [detail, agentFilter])
  const agentFindings = findings.filter((finding) => finding.scope_type === 'agent')
  const overviewFindings = findings.filter((finding) => finding.scope_type !== 'agent')
  const relatedLessons = useMemo(() => {
    const lessons = lessonsQuery.data?.data ?? []
    return selectRelatedLessons(lessons, detail)
  }, [detail, lessonsQuery.data])
  const marketFinding = overviewFindings.find((finding) => finding.scope_type === 'market_event')
  const decisionChain = meeting?.workflow_steps.length
    ? meeting.workflow_steps
    : detail?.decision_chain ?? []
  const selectedMetric = detail?.metrics.find((metric) => metric.horizon === detail.horizon)
    ?? detail?.metrics[0]
  const sourceTimeline = useMemo(
    () => [...(detail?.source_timeline ?? [])].sort((left, right) => {
      const leftTime = left.event_time ?? left.published_at ?? left.ingested_at ?? ''
      const rightTime = right.event_time ?? right.published_at ?? right.ingested_at ?? ''
      return leftTime.localeCompare(rightTime)
    }),
    [detail],
  )

  function selectReflection(id: string) {
    const next = new URLSearchParams(searchParams)
    next.set('id', id)
    setSearchParams(next)
  }

  if (listQuery.isLoading) return <LoadingPanel />

  return (
    <div className="page reflections-page">
      <PageHeading
        eyebrow="结果后学习"
        title="每日反省"
        description="用真实行情核对昨日判断，区分可避免遗漏、截止后冲击与未决归因；经验先进入候选区，不回写历史预测。"
        actions={
          <div className="reflection-heading-note">
            <BrainCircuit size={18} />
            <div><span>学习边界</span><strong>先复盘，后晋升</strong></div>
          </div>
        }
      />
      {listQuery.data?.mode === 'demo' && (
        <DemoBanner error={listQuery.data.error} reason={listQuery.data.demo_reason} />
      )}

      {listQuery.isError ? (
        <>
          <div className="action-message error" role="alert">{listQuery.error.message}</div>
          <EmptyState title="反省记录不可用" description="没有使用静态 Demo 伪造历史复盘，请确认后端已完成到期评价。" />
        </>
      ) : reflections.length === 0 ? (
        <EmptyState
          title={runFilter ? '该投委会尚无反省记录' : '尚无反省记录'}
          description="预测到期、可信行情完成评价并通过完整性校验后，系统才会生成反省。"
        />
      ) : (
        <div className="reflection-layout">
          <aside className="panel reflection-index">
            <div className="reflection-index-head">
              <div><span className="eyebrow">复盘记录</span><h2>复盘批次</h2></div>
              <small>{reflections.length} 份</small>
            </div>
            <div className="reflection-list">
              {reflections.map((item) => (
                <button
                  type="button"
                  key={item.id}
                  className={item.id === selectedId ? 'active' : ''}
                  onClick={() => selectReflection(item.id)}
                >
                  <div className="reflection-list-top">
                    <strong>{item.target_date || '日期待确认'} · {item.horizon ?? '跨周期'}</strong>
                    <SeverityBadge severity={item.severity} />
                  </div>
                  <span>{item.source_run_id}</span>
                  <p>{item.summary}</p>
                  <footer>
                    <ReflectionStatusBadge status={item.status} />
                    <span>{item.finding_count} 条发现 · {item.lesson_candidate_count} 条经验</span>
                  </footer>
                  {item.supersedes_id && (
                    <div className="revision-lineage">修订自 {item.supersedes_id}</div>
                  )}
                </button>
              ))}
            </div>
          </aside>

          <div className="reflection-detail">
            {detailQuery.isLoading ? <LoadingPanel size="section" /> : detailQuery.isError ? (
              <div className="panel reflection-error">
                <div className="action-message error" role="alert">{detailQuery.error.message}</div>
                <EmptyState title="复盘详情不可用" description="列表仍可浏览，但该批次的审计详情未能通过接口校验。" />
              </div>
            ) : detail ? (
              <>
                <section className={`panel reflection-hero ${detail.severity}`}>
                  <div className="reflection-hero-copy">
                    <div className="reflection-hero-kicker">
                      <SeverityBadge severity={detail.severity} />
                      {detail.systemic && <span className="systemic-tag"><AlertTriangle size={12} /> 系统性共振</span>}
                      <ReflectionStatusBadge status={detail.status} />
                    </div>
                    <h2>{marketFinding?.attribution ?? detail.summary}</h2>
                    <p>{severityDescription[detail.severity]}</p>
                  </div>
                  <div className="reflection-hero-meta">
                    <span>目标日<strong>{detail.target_date || '—'}</strong></span>
                    <span>原运行<Link to={`/meeting/${detail.source_run_id}`}>{detail.source_run_id}</Link></span>
                    <span>反省完成<strong>{detail.completed_at ? formatDateTime(detail.completed_at) : '—'}</strong></span>
                    <span>
                      修订血缘
                      {detail.supersedes_id
                        ? <Link to={`/reflections?id=${detail.supersedes_id}`}>修订自 {detail.supersedes_id}</Link>
                        : <strong>首版</strong>}
                    </span>
                  </div>
                </section>

                <section className="reflection-metrics">
                  <div className="metric-card featured">
                    <div className="metric-icon"><AlertTriangle size={19} /></div>
                    <span>行情严重度</span>
                    <strong className="metric-name">{severityLabel[detail.severity]}</strong>
                    <small>{detail.systemic ? '五指数或市场宽度出现系统性共振' : '按冻结阈值与历史分位判定'}</small>
                  </div>
                  <div className="metric-card">
                    <div className="metric-icon"><Target size={19} /></div>
                    <span>{selectedMetric?.horizon ?? detail.horizon ?? '所选周期'} 符号命中率</span>
                    <strong>{percent(selectedMetric?.sign_accuracy)}</strong>
                    <small>
                      {selectedMetric?.sign_sample_size
                        ? `${selectedMetric.sign_correct}/${selectedMetric.sign_sample_size} 命中`
                        : '无可评分符号样本'}
                    </small>
                  </div>
                  <div className="metric-card">
                    <div className="metric-icon"><Target size={19} /></div>
                    <span>{selectedMetric?.horizon ?? detail.horizon ?? '所选周期'} 重大行情命中率</span>
                    <strong>{percent(selectedMetric?.material_direction_accuracy)}</strong>
                    <small>
                      {selectedMetric?.material_sample_size
                        ? `${selectedMetric.material_correct}/${selectedMetric.material_sample_size} 命中 · 仅统计噪声带外`
                        : '无噪声带外样本'}
                    </small>
                  </div>
                  <div className="metric-card">
                    <div className="metric-icon"><GitCompareArrows size={19} /></div>
                    <span>{selectedMetric?.horizon ?? detail.horizon ?? '所选周期'} 三分类 Brier</span>
                    <strong>
                      {selectedMetric?.average_brier === null
                        || selectedMetric?.average_brier === undefined
                        ? '—'
                        : selectedMetric.average_brier.toFixed(3)}
                    </strong>
                    <small>
                      {selectedMetric?.outcome_count
                        ? `${selectedMetric.outcome_count} 个预测结果`
                        : '无可校准样本'}
                    </small>
                  </div>
                  <div className="metric-card">
                    <div className="metric-icon"><Lightbulb size={19} /></div>
                    <span>经验候选</span>
                    <strong>{detail.lesson_candidate_ids.length}</strong>
                    <small>不会在同一次预测中自动进入 Wiki</small>
                  </div>
                </section>

                <section className="panel outcome-matrix-panel">
                  <div className="panel-heading">
                    <div><span className="eyebrow">结果核对</span><h2>预测 / 实际矩阵</h2></div>
                    <span className="subtle-text">严重度来自冻结规则，不由事后叙事决定</span>
                  </div>
                  {detail.outcomes.length ? (
                    <div className="table-scroll">
                      <table className="data-table outcome-matrix">
                        <thead>
                          <tr>
                            <th>指数</th><th>周期</th><th>预测</th><th>实际</th><th>实际收益</th>
                            <th>标准化</th><th>严重度</th><th>结论</th>
                          </tr>
                        </thead>
                        <tbody>
                          {detail.outcomes.map((outcome) => (
                            <tr key={outcome.forecast_id}>
                              <td>
                                <div className="outcome-index">
                                  <strong>{outcome.index_name || INDEXES.find((item) => item.code === outcome.index_code)?.name}</strong>
                                  <small>{outcome.index_code}</small>
                                </div>
                              </td>
                              <td>{outcome.horizon}</td>
                              <td><DirectionBadge direction={outcome.predicted_direction} subtle /></td>
                              <td><DirectionBadge direction={outcome.actual_label} /></td>
                              <td className={outcome.actual_return < 0 ? 'negative-value' : 'positive-value'}>
                                {signedPercent(outcome.actual_return)}
                              </td>
                              <td>{outcome.signed_sigma === undefined ? '—' : `${outcome.signed_sigma.toFixed(2)}σ`}</td>
                              <td><SeverityBadge severity={outcome.severity} /></td>
                              <td className={outcome.correct === null ? 'outcome-noise' : outcome.correct ? 'outcome-correct' : 'outcome-wrong'}>
                                {outcome.correct === null
                                  ? '噪声，不计方向'
                                  : outcome.correct
                                    ? <><CheckCircle2 size={13} /> 命中</>
                                    : <><XCircle size={13} /> 误判</>}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : <EmptyState title="尚无已评价结果" description="不会在缺少可信收盘行情时推断实际涨跌。" />}
                </section>

                <section className="panel market-context-panel">
                  <div className="panel-heading">
                    <div><span className="eyebrow">市场背景</span><h2>收盘宽度与贡献</h2></div>
                    <span className="subtle-text">来自冻结 MarketSessionSnapshot</span>
                  </div>
                  {detail.outcomes.length ? (
                    <div className="market-context-grid">
                      {detail.outcomes.map((outcome) => (
                        <article key={`context-${outcome.forecast_id}`}>
                          <header>
                            <div><strong>{outcome.index_name}</strong><small>{outcome.horizon}</small></div>
                            {outcome.data_incomplete && <span>data_incomplete</span>}
                          </header>
                          <dl>
                            <div><dt>下跌宽度</dt><dd>{percent(outcome.breadth_down_ratio)}</dd></div>
                            <div><dt>涨 / 跌 / 平</dt><dd>
                              {outcome.advancers ?? '—'} / {outcome.decliners ?? '—'} / {outcome.unchanged ?? '—'}
                            </dd></div>
                            <div><dt>跌停数</dt><dd>{outcome.limit_down_count ?? '—'}</dd></div>
                            <div><dt>历史样本</dt><dd>{outcome.history_sample_size ?? '—'} 日</dd></div>
                          </dl>
                          <div className="contribution-groups">
                            <div>
                              <strong>行业贡献</strong>
                              {outcome.sector_contributions.length
                                ? <ul>{outcome.sector_contributions.slice(0, 3).map((item, index) => (
                                    <li key={`${outcome.forecast_id}-sector-${index}`}>
                                      {contributionLabel(item)}
                                    </li>
                                  ))}</ul>
                                : <span>未提供</span>}
                            </div>
                            <div>
                              <strong>权重贡献</strong>
                              {outcome.weight_contributions.length
                                ? <ul>{outcome.weight_contributions.slice(0, 3).map((item, index) => (
                                    <li key={`${outcome.forecast_id}-weight-${index}`}>
                                      {contributionLabel(item)}
                                    </li>
                                  ))}</ul>
                                : <span>未提供</span>}
                            </div>
                          </div>
                        </article>
                      ))}
                    </div>
                  ) : (
                    <EmptyState title="行情快照不可用" description="缺失时保持空白，不从复盘叙事推断市场宽度。" />
                  )}
                </section>

                <section className="panel source-timeline-panel">
                  <div className="panel-heading">
                    <div><span className="eyebrow">冻结来源</span><h2>事后来源时间线</h2></div>
                    <span className="subtle-text">只展示确定性冻结并校验哈希的来源</span>
                  </div>
                  {sourceTimeline.length ? (
                    <div className="source-timeline">
                      {sourceTimeline.map((source) => (
                        <article id={`reflection-source-${source.id}`} key={source.id}>
                          <div className="source-timeline-marker"><Clock3 size={13} /></div>
                          <div className="source-timeline-body">
                            <div>
                              <span className={`source-time-class ${source.time_class}`}>
                                {sourceTimeLabel[source.time_class]}
                              </span>
                              {source.source_kind && <small>{source.source_kind}</small>}
                            </div>
                            <h3>{source.title}</h3>
                            {source.summary && <p>{source.summary}</p>}
                            <footer>
                              <span>
                                发布时间
                                <strong>
                                  {source.published_at
                                    ? formatDateTime(source.published_at, true)
                                    : '未确认'}
                                </strong>
                              </span>
                              {source.related_index_codes.length > 0 && (
                                <span>{source.related_index_codes.join(' · ')}</span>
                              )}
                              <a href={source.source_url} target="_blank" rel="noreferrer">
                                查看来源 <ExternalLink size={11} />
                              </a>
                            </footer>
                          </div>
                        </article>
                      ))}
                    </div>
                  ) : (
                    <EmptyState
                      title="原因尚未证实"
                      description="本批次没有可展示的已冻结事后来源；不会用未冻结报道补写因果。"
                    />
                  )}
                </section>

                <div className="reflection-analysis-grid">
                  <section className="panel reflection-chain-panel">
                    <div className="panel-heading">
                      <div><span className="eyebrow">决策追踪</span><h2>决策链复原</h2></div>
                    </div>
                    {decisionChain.length ? (
                      <div className="reflection-chain">
                        {decisionChain.map((step, index) => (
                          <div key={step.id} className="reflection-chain-step">
                            <span>{index + 1}</span>
                            <div><strong>{step.label}</strong><p>{step.detail ?? '已绑定原运行记录'}</p></div>
                            {index < decisionChain.length - 1 && <ArrowRight size={14} />}
                          </div>
                        ))}
                      </div>
                    ) : <EmptyState title="决策链不可用" description="未使用通用故事替代原运行的真实工作流记录。" />}
                  </section>

                  <section className="panel time-policy-panel">
                    <div className="panel-heading">
                      <div><span className="eyebrow">时间防火墙</span><h2>时间与因果分层</h2></div>
                    </div>
                    <p>只有“截止前遗漏”可以认定为可避免错误；截止后冲击不得回填到昨日预测。</p>
                    <div className="time-policy-list">
                      {(Object.keys(availabilityLabel) as AvailabilityClass[]).map((item) => (
                        <div key={item}>
                          <span className={`availability-badge ${item}`}>{availabilityLabel[item]}</span>
                          <strong>{findings.filter((finding) => finding.availability_class === item).length}</strong>
                        </div>
                      ))}
                    </div>
                    <footer>
                      <span>预测 cutoff</span><strong>{detail.prediction_cutoff ? formatDateTime(detail.prediction_cutoff) : '见原运行'}</strong>
                      <span>反省 cutoff</span><strong>{detail.reflection_cutoff ? formatDateTime(detail.reflection_cutoff) : '—'}</strong>
                    </footer>
                  </section>
                </div>

                {overviewFindings.length > 0 && (
                  <section>
                    <div className="section-heading">
                      <div><span className="eyebrow">委员会归因</span><h2>整体归因</h2></div>
                      <span className="subtle-text">市场事实与投委会过程分开记录</span>
                    </div>
                    <div className="reflection-finding-stack overview">
                      {overviewFindings.map((finding) => (
                        <FindingCard key={finding.id} finding={finding} />
                      ))}
                    </div>
                  </section>
                )}

                <section>
                  <div className="section-heading">
                    <div><span className="eyebrow">逐项发现</span><h2>Agent 逐项反省</h2></div>
                    {agentFilter && (
                      <Link className="text-link" to={`/reflections?id=${detail.id}`}>清除 Agent 筛选</Link>
                    )}
                  </div>
                  {agentFindings.length ? (
                    <div className="reflection-finding-stack">
                      {agentFindings.map((finding) => (
                        <FindingCard
                          key={finding.id}
                          finding={finding}
                          agentName={finding.agent_id ? agentNames.get(finding.agent_id) : undefined}
                        />
                      ))}
                    </div>
                  ) : (
                    <EmptyState
                      title={agentFilter ? '该 Agent 在本批次没有发现' : '没有 Agent 级反省'}
                      description="市场与投委会级发现仍保留；不会为凑齐角色而制造归因。"
                    />
                  )}
                  <div className="quant-not-applicable" role="note">
                    <ShieldQuestion size={16} />
                    <div>
                      <strong>Quant 占位 Agent：不适用</strong>
                      <span>真实量化输入尚未接入，因此不生成伪反省，也不进入方向胜率。</span>
                    </div>
                  </div>
                </section>

                <section>
                  <div className="section-heading">
                    <div><span className="eyebrow">经验门禁</span><h2>经验候选与状态</h2></div>
                    <Link className="text-link" to="/wiki"><Lightbulb size={14} /> 查看正式 Wiki</Link>
                  </div>
                  <div className="lesson-separation-note" role="note">
                    <strong>经验激活 ≠ Wiki 晋升</strong>
                    <span>active 只表示 Lesson 已进入受控生命周期；正式 Wiki 仍需人工审核、版本晋升和审计记录。</span>
                  </div>
                  {lessonsQuery.isLoading ? <LoadingPanel size="section" /> : lessonsQuery.isError ? (
                    <div className="action-message error" role="alert">{lessonsQuery.error.message}</div>
                  ) : relatedLessons.length ? (
                    <div className="lesson-grid">
                      {relatedLessons.map((lesson) => <LessonCard key={lesson.id} lesson={lesson} />)}
                    </div>
                  ) : (
                    <EmptyState title="尚无经验候选" description="单日复盘可以没有新经验；只有可重复且通过门槛的结论才进入提案。" />
                  )}
                </section>

                <div className="method-note reflection-method-note">
                  <strong>防止事后污染：</strong>
                  <span>反省只追加新记录。截止后资料可解释真实行情，但不能伪装成昨天可见的证据；候选经验必须经过独立 episode、反例与回放检验后，才可能在未来版本晋升。</span>
                </div>
              </>
            ) : null}
          </div>
        </div>
      )}
    </div>
  )
}
