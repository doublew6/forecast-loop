import { Activity, Award, BarChart3, HelpCircle, SearchCheck, Target } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import { DemoBanner, EmptyState, LoadingPanel, PageHeading, SampleWarning } from '../components/Common'
import { useScorecards } from '../lib/api'
import { percent } from '../lib/format'
import type { Horizon } from '../lib/types'

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

export function Scorecards() {
  const query = useScorecards()
  const [horizon, setHorizon] = useState<Horizon>('D2')
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
            <div className="horizon-toggle">
              {(['D1', 'D2'] as Horizon[]).map((item) => (
                <button
                  key={item}
                  aria-pressed={horizon === item}
                  onClick={() => setHorizon(item)}
                  className={horizon === item ? 'active' : ''}
                >
                  {item}<span>{item === 'D1' ? '次日' : '两日'}</span>
                </button>
              ))}
            </div>
          </div>
        }
      />
      {query.data?.mode === 'demo' && <DemoBanner error={query.data.error} reason={query.data.demo_reason} />}

      <section className="metric-grid">
        <div className="metric-card featured"><div className="metric-icon"><Target size={19} /></div><span>投委会符号命中率</span><strong>{percent(committee?.sign_accuracy)}</strong><small>{committee?.sign_sample_size ? `${committee.sign_correct}/${committee.sign_sample_size} 命中` : '无正式 Live 样本'}</small></div>
        <div className="metric-card"><div className="metric-icon"><BarChart3 size={19} /></div><span>投委会重大行情命中率</span><strong>{percent(committee?.material_direction_accuracy)}</strong><small>{committee?.material_sample_size ? `${committee.material_correct}/${committee.material_sample_size} 命中 · 仅统计噪声带外` : '无噪声带外样本'}</small></div>
        <div className="metric-card"><div className="metric-icon"><Activity size={19} /></div><span>投委会三分类 Brier</span><strong>{committee?.brier?.toFixed(3) ?? '—'}</strong><small>{committee?.expected_calibration_error === null || committee?.expected_calibration_error === undefined ? '越低代表概率质量越好' : `ECE ${committee.expected_calibration_error.toFixed(3)}`}</small></div>
        <div className="metric-card"><div className="metric-icon"><Award size={19} /></div><span>当前最佳角色</span><strong className="metric-name">{best?.agent_name ?? '等待样本'}</strong><small>{best ? `${percent(best.material_direction_accuracy ?? best.sign_accuracy)} 重大行情命中率` : '至少需要 20 个预测截面'}</small></div>
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
