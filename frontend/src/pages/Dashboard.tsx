import { ArrowRight, CalendarDays, CheckCircle2, Clock3, Database, NotebookPen, ShieldCheck } from 'lucide-react'
import { useMemo } from 'react'
import { Link } from 'react-router'

import { DemoBanner, DirectionBadge, EmptyState, LoadingPanel, PageHeading, QualityBadge } from '../components/Common'
import { ProbabilityBar } from '../components/Probability'
import { formatDateTime, percent } from '../lib/format'
import { useLatestForecasts, useMarketUniverse, useMeeting, usePredictionStatus } from '../lib/api'
import type { Forecast, PredictionDailyState } from '../lib/types'

function ForecastRow({ forecast, meetingHref }: { forecast?: Forecast; meetingHref: string }) {
  if (!forecast) {
    return (
      <article className="forecast-row unavailable">
        <span>等待预测</span>
      </article>
    )
  }

  return (
    <article className="forecast-row">
      <div className="forecast-identity">
        <span className="index-code">{forecast.index_code}</span>
        <div>
          <h3>{forecast.index_name}</h3>
          <span>{forecast.target_date ? `目标日 ${forecast.target_date}` : `噪声带 ±${percent(forecast.threshold, 2)}`}</span>
        </div>
      </div>
      <div className="forecast-call">
        <DirectionBadge direction={forecast.direction} />
        <div className="forecast-confidence">
          <span>置信度</span>
          <strong>{percent(forecast.confidence)}</strong>
        </div>
      </div>
      <div className="forecast-distribution">
        <ProbabilityBar probabilities={forecast.probabilities} />
      </div>
      <p className="forecast-rationale">{forecast.rationale}</p>
      <div className="forecast-proof">
        <span>{forecast.citations.length} 条已绑定引用</span>
        <Link to={meetingHref} aria-label={`查看 ${forecast.index_name} 的会议详情`}>
          查看证据 <ArrowRight size={14} aria-hidden="true" />
        </Link>
      </div>
    </article>
  )
}

export function Dashboard() {
  const horizon = 'D1'
  const latest = useLatestForecasts()
  const marketUniverse = useMarketUniverse()
  const predictionStatus = usePredictionStatus()
  const batch = latest.data?.data
  const todayStatus = predictionStatus.data?.data.today
  const recentPredictionHistory = predictionStatus.data?.data.history.slice(0, 5) ?? []
  const meeting = useMeeting(batch?.run_id, {
    allowDemoFallback: latest.data?.demo_reason === 'fallback',
  })
  const selected = useMemo(
    () => new Map(batch?.forecasts.filter((forecast) => forecast.horizon === horizon).map((item) => [item.index_code, item])),
    [batch, horizon],
  )
  const instruments = useMemo(() => {
    if (batch?.forecasts.length) {
      return [...new Map(
        batch.forecasts.map((forecast) => [
          forecast.index_code,
          { code: forecast.index_code, name: forecast.index_name },
        ]),
      ).values()]
    }
    return marketUniverse.data?.data.instruments ?? []
  }, [batch, marketUniverse.data])
  const primaryInstrument = instruments[0]
  const forecasts = [...selected.values()]
  const averageConfidence = forecasts.length
    ? forecasts.reduce((sum, item) => sum + item.confidence, 0) / forecasts.length
    : 0
  const activeOpinions = useMemo(() => {
    const eligible = meeting.data?.data.opinions.filter(
      (opinion) =>
        opinion.status === 'active'
        && opinion.horizon === horizon
        && opinion.index_code === primaryInstrument?.code,
    ) ?? []
    return [...new Map(eligible.map((opinion) => [opinion.agent_id, opinion])).values()]
  }, [meeting.data, horizon, primaryInstrument?.code])
  const demoMode = latest.data?.mode === 'demo' || meeting.data?.mode === 'demo'
  const meetingError = meeting.isError
    ? `运行 ${batch?.run_id ?? '—'} 的会议详情加载失败：${meeting.error.message}。未使用其他运行或静态 Demo 意见替代。`
    : undefined
  const meetingHref = latest.data?.demo_reason === 'fallback'
    ? '/meeting'
    : `/meeting/${encodeURIComponent(batch?.run_id ?? '')}`
  const statusLabels: Record<PredictionDailyState, string> = {
    pending: '今日任务待运行',
    stale: '今日任务缺跑',
    holiday: '今日休市',
    blocked: '今日预测已阻断',
    awaiting: '今日预测等待完成',
    overdue: '今日预测已逾期',
    completed: '今日研究已冻结',
  }
  const historyStateLabels: Record<PredictionDailyState, string> = {
    pending: '待运行',
    stale: '缺跑',
    holiday: '休市',
    blocked: '阻断',
    awaiting: '等待完成',
    overdue: '逾期',
    completed: '完成',
  }
  const statusMessage = predictionStatus.isError
    ? '无法读取每日预测准备回执。'
    : todayStatus && todayStatus.state !== 'completed'
      ? todayStatus.message
      : undefined
  const liveIndicator = todayStatus
    ? statusLabels[todayStatus.state]
    : '正在核对今日任务状态'

  if (latest.isLoading) return <LoadingPanel />

  return (
    <div className="page dashboard-page">
      <PageHeading
        eyebrow="预测账本"
        title={todayStatus?.state === 'completed' ? '今日投委会决策' : '最近交易日投委会决策'}
        description="核对下一交易日预测、证据新鲜度与今日任务进度。"
        actions={
          <div className="as-of-block">
            <CalendarDays size={17} />
            <div>
              <span>决策时间</span>
              <strong>{batch ? formatDateTime(batch.as_of) : '—'}</strong>
            </div>
          </div>
        }
      />

      {demoMode && (
        <DemoBanner
          dataAsOf={batch?.as_of}
          error={latest.data?.error ?? meeting.data?.error}
          reason={latest.data?.demo_reason ?? meeting.data?.demo_reason}
        />
      )}
      {meetingError && <div className="action-message error" role="alert">{meetingError}</div>}
      {statusMessage && (
        <div
          className={`action-message ${todayStatus?.state === 'holiday' || todayStatus?.state === 'pending' ? '' : 'error'}`}
          role={todayStatus?.state === 'holiday' || todayStatus?.state === 'pending' ? 'status' : 'alert'}
          data-testid="prediction-daily-status"
        >
          <strong>{liveIndicator}</strong>：{statusMessage}
        </div>
      )}

      <section className="overview-strip" aria-label="运行概览">
        <div className="overview-primary">
          <div className={`live-indicator ${todayStatus?.state ?? 'pending'}`}><i /> {liveIndicator}</div>
          <strong>下一交易日方向预测</strong>
          <span>预测只用于研究评估，不构成交易指令</span>
        </div>
        <div className="overview-metric">
          <Database size={18} />
          <div><span>数据截止</span><strong>{batch ? formatDateTime(batch.data_cutoff) : '—'}</strong></div>
        </div>
        <div className="overview-metric">
          <ShieldCheck size={18} />
          <div>
            <span>证据校验</span>
            <strong>
              {meeting.data?.data.run.data_quality_details?.citations_validated !== undefined
                ? `${meeting.data.data.run.data_quality_details.citations_validated} 条已验证`
                : '尚无校验结果'}
            </strong>
          </div>
        </div>
        <div className="overview-metric">
          <CheckCircle2 size={18} />
          <div><span>平均涨跌置信度</span><strong>{percent(averageConfidence)}</strong></div>
        </div>
      </section>

      <div className="section-heading">
        <div>
          <span className="eyebrow">封签结果</span>
          <h2 id="forecast-board-heading">标的预测矩阵</h2>
        </div>
      </div>

      <section className="forecast-board panel" aria-labelledby="forecast-board-heading">
        <div className="forecast-board-head" aria-hidden="true">
          <span>预测标的</span>
          <span>方向 / 置信度</span>
          <span>概率分布</span>
          <span>核心理由</span>
          <span>证据</span>
        </div>
        {instruments.map((instrument) => (
          <ForecastRow key={instrument.code} forecast={selected.get(instrument.code)} meetingHref={meetingHref} />
        ))}
      </section>

      <section className="dashboard-lower-grid">
        <div className="panel committee-panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">决策来源</span>
              <h2>委员涨跌立场</h2>
              <span className="panel-caption">
                {primaryInstrument?.name ?? '首个预测标的'} · {horizon} · 每个角色一条意见
              </span>
            </div>
            <div className="committee-links">
              <Link className="text-link" to="/judgments" title="本页已展示委员会结论，从这里进入请勿声明盲判">
                <NotebookPen size={14} /> 记录判断（本页已揭晓）
              </Link>
              <Link className="text-link" to={meetingHref}>查看会议全貌 <ArrowRight size={15} /></Link>
            </div>
          </div>
          <div className="agent-opinion-list">
            {meeting.isLoading ? <LoadingPanel size="section" /> : meeting.isError ? (
              <EmptyState title="委员意见不可用" description={`未能读取运行 ${batch?.run_id ?? '—'} 的会议记录。`} />
            ) : activeOpinions.length ? (
              activeOpinions.map((opinion) => (
                <div className="agent-opinion-row" key={opinion.id}>
                  <div className="agent-avatar">{opinion.agent_name.slice(0, 1)}</div>
                  <div className="agent-opinion-main">
                    <div><strong>{opinion.agent_name}</strong><span>{opinion.role}</span></div>
                    <p>{opinion.summary}</p>
                  </div>
                  <div className="agent-opinion-side">
                    <DirectionBadge direction={opinion.direction} subtle />
                    <span>{opinion.contribution}</span>
                  </div>
                </div>
              ))
            ) : <EmptyState title="暂无委员意见" description={`运行 ${batch?.run_id ?? '—'} 未返回 ${primaryInstrument?.name ?? '首个预测标的'} · ${horizon} 的有效意见。`} />}
          </div>
        </div>

        <div className="panel integrity-panel">
          <div className="panel-heading">
            <div><span className="eyebrow">证据状态</span><h2>可验证性</h2></div>
            {!meeting.isError && <QualityBadge quality={meeting.data?.data.run.data_quality ?? 'warning'} />}
          </div>
          {meeting.isLoading ? <LoadingPanel size="section" /> : meeting.isError ? (
            <EmptyState title="校验结果不可用" description={`未能读取运行 ${batch?.run_id ?? '—'} 的 Evidence Validator 结果。`} />
          ) : (
            <>
              <div className="integrity-score">
                <div className="score-ring">
                  <strong>{meeting.data?.data.run.data_quality_details?.citations_validated ?? '—'}</strong>
                  <span> 条</span>
                </div>
                <div><strong>已验证引用</strong><span>只展示 Evidence Validator 返回的结果</span></div>
              </div>
              <div className="integrity-list">
                <div><CheckCircle2 size={16} /><span>Wiki 条目快照</span><strong>{meeting.data?.data.run.data_quality_details?.wiki_entries ?? '—'} 个</strong></div>
                <div><CheckCircle2 size={16} /><span>带原始来源的 Wiki</span><strong>{meeting.data?.data.run.data_quality_details?.wiki_has_sources ?? '—'} 个</strong></div>
                <div><CheckCircle2 size={16} /><span>未来信息检查</span><strong>{meeting.data?.data.run.data_quality_details?.future_information_check === 'passed' ? '通过' : '未确认'}</strong></div>
                <div><Clock3 size={16} /><span>运行耗时</span><strong>{meeting.data?.data.run.duration_seconds ?? '—'} 秒</strong></div>
              </div>
            </>
          )}
        </div>
      </section>

      {recentPredictionHistory.length > 0 && (
        <section className="prediction-history panel" aria-labelledby="prediction-history-heading">
          <div className="prediction-history-heading">
            <div>
              <span className="eyebrow">任务回执</span>
              <h2 id="prediction-history-heading">每日运行记录</h2>
            </div>
            <span>最近 {recentPredictionHistory.length} 个有回执日期</span>
          </div>
          <ol>
            {recentPredictionHistory.map((attempt) => (
              <li key={attempt.attempt_id}>
                <time dateTime={attempt.base_session}>{attempt.base_session}</time>
                <strong className={`prediction-history-state ${attempt.state}`}>
                  {historyStateLabels[attempt.state]}
                </strong>
                <p>{attempt.message ?? '该日准备回执未提供公开说明。'}</p>
                <span>{formatDateTime(attempt.attempted_at)}</span>
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  )
}
