import { Activity, BarChart3, BrainCircuit, GitCompareArrows, HelpCircle, SearchCheck, Target, Waypoints } from 'lucide-react'
import { useMemo } from 'react'
import { Link } from 'react-router'
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import { DemoBanner, EmptyState, LoadingPanel, PageHeading, SampleWarning } from '../components/Common'
import { useAgentScorecardsV2, useReasoningReviewsV2, useScorecards } from '../lib/api'
import { percent, signedPercent } from '../lib/format'
import type { AgentScorecardAxisV2, AgentScorecardSectionV2, PremarketHistoryPoint } from '../lib/types'

const workflowRoleLabel = {
  research: 'Research',
  strategy: 'Strategy',
  critic: 'Critic',
  decision: 'Decision',
  shadow: 'Shadow',
} as const

const sourceTypeLabel = {
  ai: 'AI',
  manual: 'Manual',
  quant: 'Quant',
  deterministic: 'Rule',
} as const

function LegacyScorecards() {
  const query = useScorecards()
  const horizon = 'D1' as const
  const rows = useMemo(
    () => query.data?.data.filter((row) => row.horizon === horizon) ?? [],
    [query.data, horizon],
  )
  const scored = rows.filter(
    (row) => (
      row.sample_size > 0
      && (row.sign_accuracy !== null || row.material_direction_accuracy !== null)
    ),
  )
  const comparable = scored.filter((row) => row.sample_sufficient)
  const best = [...comparable].sort(
    (a, b) => (
      (b.material_direction_accuracy ?? b.sign_accuracy ?? 0)
      - (a.material_direction_accuracy ?? a.sign_accuracy ?? 0)
    ),
  )[0]
  const committee = rows.find((row) => row.agent_id === 'cio_agent')
  const calibration = committee?.sample_sufficient ? committee.calibration : []

  if (query.isLoading) return <LoadingPanel />

  return (
    <div className="page scorecard-page">
      <PageHeading
        eyebrow="历史检验"
        title="Agent 历史成绩单"
        description="同时保留符号命中率、重大行情命中率与三分类 Brier，避免用单一正确率掩盖噪声和校准问题。"
        actions={
          <div className="scorecard-heading-actions">
            <Link className="secondary-button" to="/reflections">
              <SearchCheck size={15} /> 查看每日反省
            </Link>
            <div className="horizon-identity compact" aria-label="成绩周期：下一交易日">
              <span>成绩周期</span>
              <strong>D1</strong>
              <small>下一交易日</small>
            </div>
          </div>
        }
      />
      {query.data?.mode === 'demo' && <DemoBanner error={query.data.error} reason={query.data.demo_reason} />}

      <section className="metric-grid">
        <div className="metric-card featured"><div className="metric-icon"><Target size={19} /></div><span>投委会符号命中率</span><strong>{percent(committee?.sign_accuracy)}</strong><small>{committee?.sign_sample_size ? `${committee.sign_correct}/${committee.sign_sample_size} 命中` : '无正式 Live 样本'}</small></div>
        <div className="metric-card"><div className="metric-icon"><BarChart3 size={19} /></div><span>投委会重大行情命中率</span><strong>{percent(committee?.material_direction_accuracy)}</strong><small>{committee?.material_sample_size ? `${committee.material_correct}/${committee.material_sample_size} 命中 · 仅统计噪声带外` : '无噪声带外样本'}</small></div>
        <div className="metric-card"><div className="metric-icon"><Activity size={19} /></div><span>投委会三分类 Brier</span><strong>{committee?.brier?.toFixed(3) ?? '—'}</strong><small>{committee?.expected_calibration_error === null || committee?.expected_calibration_error === undefined ? '越低代表概率质量越好' : `ECE ${committee.expected_calibration_error.toFixed(3)}`}</small></div>
        <div className="metric-card"><div className="metric-icon"><Waypoints size={19} /></div><span>可比较角色</span><strong>{comparable.length}</strong><small>{best ? `已有 ${comparable.length} 个角色达到样本门槛` : '至少需要 20 个预测截面'}</small></div>
      </section>

      <section className="scorecard-grid">
        <div className="panel scorecard-table-panel">
          <div className="panel-heading">
            <div><span className="eyebrow">横向比较</span><h2>角色表现</h2></div>
            <div className="metric-help"><HelpCircle size={14} /> 立场只选涨跌，实际结果保留小波动桶</div>
          </div>
          {rows.length ? (
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>Agent</th><th>符号命中率</th><th>重大行情命中率</th><th>三分类 Brier</th><th>上涨精度</th><th>下跌精度</th><th>样本</th></tr></thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={`${row.agent_id}-${row.horizon}`}>
                      <td><div className="table-agent"><span>{row.agent_name.slice(0, 1)}</span><div><strong>{row.agent_name}</strong><small>{row.role}</small><small>{workflowRoleLabel[row.workflow_role]} · 注册来源 {sourceTypeLabel[row.source_type]}</small><small title={row.model_name ?? undefined}>v{row.agent_version} · {row.model_name ?? '未记录模型'}</small></div></div></td>
                      <td className="primary-cell">
                        <div className="sample-detail">
                          <span>{percent(row.sign_accuracy)}</span>
                          <small>{row.sign_sample_size ? `${row.sign_correct}/${row.sign_sample_size}` : '无样本'}</small>
                        </div>
                      </td>
                      <td>
                        <div className="sample-detail">
                          <span>{percent(row.material_direction_accuracy)}</span>
                          <small>{row.material_sample_size ? `${row.material_correct}/${row.material_sample_size}` : '无重大行情样本'}</small>
                        </div>
                      </td>
                      <td>{row.brier?.toFixed(3) ?? '—'}</td>
                      <td>{percent(row.up_precision)}</td>
                      <td>{percent(row.down_precision)}</td>
                      <td><div className="sample-detail"><span>{row.sample_size} {!row.sample_sufficient && <SampleWarning />}</span><small>{row.note}</small></div></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <EmptyState title={`${horizon} 暂无成绩`} description="预测到期并完成评分后会出现在这里。" />}
        </div>

        <div className="panel calibration-panel">
          <div className="panel-heading"><div><span className="eyebrow">概率检验</span><h2>投委会概率校准</h2></div></div>
          <p className="chart-note">预测概率越接近实际发生率，越接近虚线。</p>
          {calibration.length ? (
            <div className="calibration-chart" aria-label="概率校准曲线">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={calibration} margin={{ top: 8, right: 8, left: -18, bottom: 2 }}>
                  <CartesianGrid stroke="#e6e2d9" strokeDasharray="3 4" vertical={false} />
                  <XAxis dataKey="bucket" tick={{ fontSize: 11, fill: '#7c796f' }} axisLine={false} tickLine={false} />
                  <YAxis domain={[0.2, 0.8]} tickFormatter={(value) => `${Math.round(value * 100)}%`} tick={{ fontSize: 11, fill: '#7c796f' }} axisLine={false} tickLine={false} />
                  <Tooltip formatter={(value: number) => percent(value)} contentStyle={{ borderRadius: 8, border: '1px solid #ddd8cc', fontSize: 12 }} />
                  <Line type="monotone" dataKey="predicted" name="理想概率" stroke="#aaa69c" strokeDasharray="4 4" dot={false} />
                  <Line type="monotone" dataKey="observed" name="实际发生率" stroke="#1f6759" strokeWidth={2.5} dot={{ r: 4, fill: '#1f6759' }} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          ) : <EmptyState title="尚无校准数据" description="至少覆盖 20 个预测截面后展示。" />}
          <div className="calibration-summary">
            <span><i className="actual" />实际发生率</span><span><i className="ideal" />理想校准线</span>
          </div>
        </div>
      </section>

      <div className="method-note">
        <strong>如何解读：</strong>
        <span>符号命中率统计所有非零实际收益的涨跌符号；重大行情命中率只统计实际离开预测噪声带的样本；三分类 Brier 同时评价上涨、噪声区和下跌的概率质量。上涨/下跌精度用于查看两种立场的偏差。表格汇总五个指数，同一天的五个结果只算一个预测截面；少于 20 个预测截面时不比较 Agent 能力。Demo 永远不进入正式成绩，Live、Agent 版本、实际模型与预测周期分别统计。</span>
      </div>
    </div>
  )
}

const axisMeta: Array<{
  axis: AgentScorecardAxisV2
  title: string
  description: string
  icon: typeof Target
}> = [
  { axis: 'final_system', title: '最终系统', description: 'Strategy 与 CIO 的正式概率结果', icon: Target },
  { axis: 'natural_horizon', title: '自然周期', description: 'D1、W1、D20 按各自周期评分', icon: Waypoints },
  { axis: 'd1_impact', title: 'D1 边际影响', description: '结构、映射与弃权，不直接算 Brier', icon: Activity },
  { axis: 'reasoning', title: '推理质量', description: '结构规则与结果揭晓前盲审', icon: BrainCircuit },
  { axis: 'incremental_value', title: '增量贡献', description: '离线 ablation 诊断，不自动调权', icon: GitCompareArrows },
]

function axisKey(axis: string) {
  const aliases: Record<string, AgentScorecardAxisV2> = {
    outcome: 'final_system',
    system: 'final_system',
    natural: 'natural_horizon',
    impact: 'd1_impact',
    ablation: 'incremental_value',
    incremental: 'incremental_value',
  }
  return aliases[axis] ?? axis
}

function score(value: number | null | undefined, digits = 3) {
  return value === null || value === undefined ? '—' : value.toFixed(digits)
}

function classwiseEce(item: AgentScorecardSectionV2['items'][number]) {
  if (!item.classwise_ece) return '—'
  const values = Object.values(item.classwise_ece).filter((value): value is number => typeof value === 'number')
  return values.length ? (values.reduce((sum, value) => sum + value, 0) / values.length).toFixed(3) : '—'
}

function ScorecardAxisTable({ section }: { section: AgentScorecardSectionV2 }) {
  if (!section.items.length) {
    return <EmptyState title={`${section.title}暂无样本`} description="v2 只展示按目标、周期和 Agent 版本隔离后的独立 episode。" />
  }
  return (
    <div className="table-scroll">
      <table className="data-table v2-score-table">
        <thead><tr><th>Agent / 信号</th><th>目标 / 周期</th><th>结果质量</th><th>推理</th><th>增量</th><th>独立样本</th></tr></thead>
        <tbody>
          {section.items.map((item) => (
            <tr key={`${item.agent_id}:${item.agent_version}:${item.model_name}:${item.prompt_version}:${item.target_id}:${item.signal_kind}:${item.horizon}`}>
              <td><div className="table-agent"><span>{item.agent_name.slice(0, 1)}</span><div><strong>{item.agent_name}</strong><small>{item.signal_kind}</small><small><code>{item.agent_id}</code></small><small>v{item.agent_version ?? '—'} · {item.model_name ?? 'unknown model'} · {item.prompt_version ?? 'unknown prompt'}</small></div></div></td>
              <td><div className="sample-detail"><span>{item.horizon}</span><small>{item.target_id}</small></div></td>
              <td><div className="sample-detail"><span>{item.risk_diagnostics ? `风险覆盖 ${percent(item.risk_diagnostics.counter_evidence_coverage_rate)}` : `Brier ${score(item.average_brier)}`}</span><small>{item.risk_diagnostics ? `失效条件 ${percent(item.risk_diagnostics.invalidation_coverage_rate)}` : `Skill ${percent(item.brier_skill, 1)} · classwise ECE avg ${classwiseEce(item)}`}</small></div></td>
              <td><div className="sample-detail"><span>{item.reasoning_average === null ? '—' : `${item.reasoning_average.toFixed(1)} / 10`}</span><small>{item.risk_diagnostics ? `漏报 ${item.risk_diagnostics.missed_risk_count} / ${item.risk_diagnostics.evaluated_system_errors} 次系统错误` : `方向 ${percent(item.direction_accuracy)}`}</small></div></td>
              <td><div className="sample-detail"><span>{item.ablation_brier_delta === null ? '—' : `${item.ablation_brier_delta >= 0 ? '+' : ''}${item.ablation_brier_delta.toFixed(4)}`}</span><small>去除 Agent 后 Brier Δ</small></div></td>
              <td><div className="sample-detail"><span>{item.independent_episodes}</span><small>{item.note || `${item.sample_size} 条记录`}</small></div></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function PremarketOutcomeLedger({ rows }: { rows: PremarketHistoryPoint[] }) {
  if (!rows.length) {
    return (
      <section className="panel premarket-outcome-ledger">
        <EmptyState title="等待首个 Open-to-Open 结算" description="只有完整封签的预测、开盘结果与评价链会进入历史成绩。" />
      </section>
    )
  }
  const latest = rows.at(-1)!
  const chartRows = rows.map((row) => ({
    ...row,
    date: row.target_session.slice(5),
    cumulative: row.cumulative_win_rate === null ? null : row.cumulative_win_rate * 100,
    rolling: row.rolling_20_win_rate === null ? null : row.rolling_20_win_rate * 100,
    longOnly: row.long_only_cumulative_return * 100,
    longShort: row.long_short_cumulative_return * 100,
  }))
  return (
    <section className="panel premarket-outcome-ledger" aria-labelledby="premarket-outcome-heading">
      <div className="panel-heading">
        <div><span className="eyebrow">Open-to-Open · Shadow 结算</span><h2 id="premarket-outcome-heading">盘前预测历史成绩</h2><span className="panel-caption">只统计噪声带外方向样本；小波动保留审计记录但不计入胜率。</span></div>
        <div className="premarket-win-seal"><span>累计胜率</span><strong>{percent(latest.cumulative_win_rate, 1)}</strong><small>{latest.cumulative_hits}/{latest.cumulative_sample_size} 命中</small></div>
      </div>
      <div className="premarket-win-chart" role="img" aria-label="中证1000 Open-to-Open 累计与滚动20次胜率曲线">
        <ResponsiveContainer width="100%" height={250}>
          <LineChart data={chartRows} margin={{ top: 18, right: 14, bottom: 4, left: -10 }}>
            <CartesianGrid stroke="var(--line-soft)" vertical={false} />
            <XAxis dataKey="date" tick={{ fill: 'var(--muted)', fontSize: 11 }} tickLine={false} axisLine={false} />
            <YAxis domain={[0, 100]} tickFormatter={(value) => `${value}%`} tick={{ fill: 'var(--muted)', fontSize: 11 }} tickLine={false} axisLine={false} />
            <Tooltip formatter={(value) => `${Number(value).toFixed(1)}%`} labelFormatter={(value) => `结算日 ${value}`} />
            <ReferenceLine y={50} stroke="var(--amber)" strokeDasharray="4 4" />
            <Line type="monotone" dataKey="cumulative" name="累计胜率" stroke="var(--green)" strokeWidth={2.25} dot={{ r: 2.5, fill: 'var(--surface)', strokeWidth: 2 }} connectNulls />
            <Line type="monotone" dataKey="rolling" name="滚动20次" stroke="var(--blue)" strokeWidth={1.5} strokeDasharray="5 4" dot={false} connectNulls />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="premarket-return-section">
        <div className="premarket-return-heading">
          <div><span className="eyebrow">复合毛收益</span><h3>策略收益曲线</h3><small>纯多头仅在偏涨时持有；多空在偏涨时做多、偏跌时做空。</small></div>
          <div className="premarket-return-metrics" aria-label="最新策略累计收益">
            <span><i className="long-only" />纯多头 <strong>{signedPercent(latest.long_only_cumulative_return, 1)}</strong></span>
            <span><i className="long-short" />多空 <strong>{signedPercent(latest.long_short_cumulative_return, 1)}</strong></span>
          </div>
        </div>
        <div className="premarket-return-chart" role="img" aria-label="中证1000 Open-to-Open 纯多头与多空策略累计毛收益曲线">
          <ResponsiveContainer width="100%" height={250}>
            <LineChart data={chartRows} margin={{ top: 18, right: 14, bottom: 4, left: -10 }}>
              <CartesianGrid stroke="var(--line-soft)" vertical={false} />
              <XAxis dataKey="date" tick={{ fill: 'var(--muted)', fontSize: 11 }} tickLine={false} axisLine={false} />
              <YAxis tickFormatter={(value) => `${Number(value).toFixed(0)}%`} tick={{ fill: 'var(--muted)', fontSize: 11 }} tickLine={false} axisLine={false} />
              <Tooltip formatter={(value) => `${Number(value).toFixed(2)}%`} labelFormatter={(value) => `结算日 ${value}`} />
              <ReferenceLine y={0} stroke="var(--amber)" strokeDasharray="4 4" />
              <Line type="monotone" dataKey="longOnly" name="纯多头" stroke="var(--green)" strokeWidth={2.25} dot={{ r: 2.5, fill: 'var(--surface)', strokeWidth: 2 }} />
              <Line type="monotone" dataKey="longShort" name="多空" stroke="var(--blue)" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <p className="premarket-return-note">毛收益按日复合；小波动信号空仓。未计手续费、滑点、融券及做空成本，不代表可交易净收益。</p>
      </div>
      <div className="table-scroll premarket-outcome-table">
        <table className="data-table">
          <thead><tr><th>区间</th><th>预测</th><th>实际收益</th><th>结果</th><th>累计胜率</th></tr></thead>
          <tbody>{[...rows].reverse().slice(0, 20).map((row) => (
            <tr key={row.forecast_hash}>
              <td><span>{row.forecast_session.slice(5)} → {row.target_session.slice(5)}</span><small>开盘 → 开盘</small></td>
              <td>{row.predicted_direction === 'up' ? '偏涨' : row.predicted_direction === 'down' ? '偏跌' : '小波动'}</td>
              <td className={row.realized_return >= 0 ? 'positive-value' : 'negative-value'}>{signedPercent(row.realized_return, 2)}</td>
              <td><span className={`outcome-verdict ${row.direction_correct === null ? 'neutral' : row.direction_correct ? 'hit' : 'miss'}`}>{row.direction_correct === null ? '小波动' : row.direction_correct ? '命中' : '未命中'}</span></td>
              <td>{percent(row.cumulative_win_rate, 1)}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </section>
  )
}

function FocusedScorecards() {
  const query = useAgentScorecardsV2()
  const reviews = useReasoningReviewsV2()
  if (query.isLoading) return <LoadingPanel />

  const received = query.data?.sections ?? []
  const sections = axisMeta.map((meta) => {
    const source = received.find((section) => axisKey(section.axis) === meta.axis)
    return { axis: meta.axis, title: meta.title, items: source?.items ?? [] }
  })
  const systemRows = sections[0].items
  const cio = systemRows.find((item) => item.signal_kind === 'decision_forecast') ?? systemRows[0]
  const reviewRows = reviews.data ?? []
  const humanQueue = reviewRows.filter(
    (item) => item.human_review_required && item.human_review_status === 'pending',
  )
  const premarketHistory = query.data?.premarket_history ?? []

  return (
    <div className="page scorecard-page focused-scorecard-page">
      <PageHeading
        eyebrow="Agent Evidence · v2"
        title="Agent 分轴成绩单"
        description="结果能力、自然周期、D1 边际影响、推理质量和增量贡献各自回答不同问题；不跨周期合并，也不生成“最佳角色”总榜。"
        actions={<Link className="secondary-button" to="/reflections"><SearchCheck size={15} />查看到期反省</Link>}
      />
      {query.isError && <div className="action-message error">v2 成绩数据不可用：{query.error.message}</div>}

      <section className="v2-axis-overview" aria-label="五项 Agent 评价轴">
        {axisMeta.map((meta, index) => {
          const Icon = meta.icon
          const count = sections[index].items.length
          return (
            <article key={meta.axis} className={meta.axis === 'final_system' ? 'featured' : ''}>
              <Icon size={17} />
              <span>{meta.title}</span>
              <strong>{count}</strong>
              <small>{meta.description}</small>
            </article>
          )
        })}
      </section>

      <section className="scorecard-v2-summary-grid">
        <div className="panel scorecard-v2-system">
          <div className="panel-heading"><div><span className="eyebrow">正式系统</span><h2>中证1000 D1</h2></div><span className="formal-lane-badge">FORMAL</span></div>
          <div className="scorecard-v2-primary-metrics">
            <div><span>平均 Brier</span><strong>{score(cio?.average_brier)}</strong><small>baseline {score(cio?.baseline_brier)}</small></div>
            <div><span>Brier Skill</span><strong>{percent(cio?.brier_skill, 1)}</strong><small>相对截止时冻结基线</small></div>
            <div><span>独立目标日</span><strong>{cio?.independent_episodes ?? 0}</strong><small>不同版本绝不串分</small></div>
          </div>
        </div>
        <div className="panel reasoning-queue-card">
          <div className="panel-heading"><div><span className="eyebrow">盲审队列</span><h2>推理审核</h2></div><BrainCircuit size={18} /></div>
          <strong>{humanQueue.length}</strong><span>条需要人工复验</span>
          <small>{reviewRows.length ? `共 ${reviewRows.length} 条结果揭晓前审核` : reviews.isError ? 'Reasoning Review 接口尚不可用' : '尚无审核记录'}</small>
        </div>
      </section>

      <PremarketOutcomeLedger rows={premarketHistory} />

      <div className="v2-scorecard-sections">
        {sections.map((section, index) => (
          <section className={`panel v2-scorecard-axis axis-${section.axis}`} key={section.axis}>
            <div className="panel-heading">
              <div><span className="eyebrow">Axis {index + 1}</span><h2>{section.title}</h2><span className="panel-caption">{axisMeta[index].description}</span></div>
              <strong className="axis-row-count">{section.items.length} 项</strong>
            </div>
            <ScorecardAxisTable section={section} />
          </section>
        ))}
      </div>

      <div className="method-note"><strong>解释边界：</strong><span>`d1_impact` 与 Risk Critic 不按市场最终涨跌计算方向胜率；ablation 只做离线诊断；盲审是 advisory。所有可信度权重保持 shadow，不因样本达标自动进入正式聚合。</span></div>
    </div>
  )
}

export function Scorecards() {
  const v2 = useAgentScorecardsV2()
  if (v2.isLoading) return <LoadingPanel />
  if (v2.data) return <FocusedScorecards />
  return <LegacyScorecards />
}
