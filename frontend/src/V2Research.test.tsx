import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'

import { App } from './App'

function response(body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }))
}

function renderRoute(route = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}><App /></MemoryRouter>
    </QueryClientProvider>,
  )
}

const program = {
  schema_version: 'forecast-loop.research-program/v2',
  program_id: 'csi1000-focused-loop',
  version: '2.0.0',
  market: 'CN',
  timezone: 'Asia/Shanghai',
  calendar_id: 'SSE',
  instruments: [
    { code: '000852.SH', name: '中证1000', role: 'primary', data_required: true },
    { code: '000300.SH', name: '沪深300', role: 'benchmark', data_required: true },
  ],
  targets: [
    { target_id: 'csi1000-absolute-d1', label: '中证1000下一交易日', outcome_kind: 'absolute_return', horizon: 'D1', lane: 'formal', primary_instrument: '000852.SH', comparison_instrument: null },
    { target_id: 'csi1000-vs-csi300-relative-w1', label: '中证1000相对沪深300未来5个交易日', outcome_kind: 'relative_return', horizon: 'W1', lane: 'shadow', primary_instrument: '000852.SH', comparison_instrument: '000300.SH' },
  ],
  research_scopes: [
    { target_id: 'csi1000-absolute-d20', label: '中证1000未来20个交易日宏观状态', horizon: 'D20', instrument: '000852.SH', lane: 'shadow' },
  ],
  program_hash: 'a'.repeat(64),
}

describe('focused v2 research UI', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('keeps the D1 formal forecast visually separate from the W1 shadow target', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation((input) => {
      const url = String(input)
      if (url.endsWith('/api/v2/research-program')) return response(program)
      if (url.endsWith('/api/v2/forecasts/latest')) return response({
        program_hash: program.program_hash,
        formal: {
          id: 'forecast-d1', target_id: 'csi1000-absolute-d1', horizon: 'D1', lane: 'formal',
          anchor_date: '2026-08-12', target_date: '2026-08-13',
          probabilities: { up: 0.48, neutral: 0.3, down: 0.22 }, neutral_threshold: 0.006,
          rationale: '正式 D1 证据略偏正向。', created_at: '2026-08-12T16:30:00+08:00',
        },
        shadow: {
          id: 'forecast-w1', target_id: 'csi1000-vs-csi300-relative-w1', horizon: 'W1', lane: 'shadow',
          anchor_date: '2026-08-10', target_date: '2026-08-14',
          probabilities: { up: 0.4, neutral: 0.35, down: 0.25 }, neutral_threshold: 0.012,
          rationale: '相对强弱仅作影子诊断。', created_at: '2026-08-10T16:30:00+08:00',
        },
      })
      return Promise.reject(new Error(`unexpected request ${url}`))
    }))

    renderRoute()

    expect(await screen.findByRole('heading', { name: '中证1000 单主标的决策台' })).toBeInTheDocument()
    const runtime = screen.getByRole('region', { name: '每日研究链路' })
    expect(await within(runtime).findByText('最新预测目标交易日')).toBeInTheDocument()
    expect(within(runtime).getByText('2026-08-12')).toBeInTheDocument()
    expect(within(runtime).getAllByText('2026-08-13')).toHaveLength(2)
    expect(within(runtime).getByText('SSE · Asia/Shanghai')).toBeInTheDocument()
    expect(within(runtime).getByText('只读取当前中证1000 D1 封签；旧版五指数回执保留在运行记录，不再回填这里。')).toBeInTheDocument()
    expect(await screen.findByText('唯一正式目标 · 已激活发布 · 下一交易日绝对涨跌')).toBeInTheDocument()
    expect(screen.getByText('SHADOW · W1')).toBeInTheDocument()
    expect(screen.getByText('只用于相对收益计算和冻结市场背景，不生成独立正式预测。')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '标的预测矩阵' })).not.toBeInTheDocument()
  })

  it('does not label the configured D1 target as active before activation', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation((input) => {
      const url = String(input)
      if (url.endsWith('/api/v2/research-program')) return response(program)
      if (url.endsWith('/api/v2/forecasts/latest')) return response({
        program_hash: program.program_hash,
        formal: {
          id: 'forecast-shadow-d1', target_id: 'csi1000-absolute-d1', horizon: 'D1',
          lane: 'shadow', configured_lane: 'formal', anchor_date: '2026-08-12', target_date: '2026-08-13',
          probabilities: { up: 0.42, neutral: 0.36, down: 0.22 }, neutral_threshold: 0.006,
          rationale: 'D1 尚处前瞻 shadow 阶段。', created_at: '2026-08-12T16:30:00+08:00',
        },
        shadow: null,
      })
      return Promise.reject(new Error(`unexpected request ${url}`))
    }))

    renderRoute()

    expect(await screen.findByText('唯一正式目标 · 当前 Shadow 观察 · 下一交易日绝对涨跌')).toBeInTheDocument()
    expect(screen.getByText('SHADOW · D1')).toBeInTheDocument()
    expect(screen.queryByText('FORMAL · D1')).not.toBeInTheDocument()
  })

  it('fails closed instead of falling back to legacy prediction receipts', async () => {
    const fetchMock = vi.fn().mockImplementation((input) => {
      const url = String(input)
      if (url.endsWith('/api/v2/research-program')) return response(program)
      if (url.endsWith('/api/v2/forecasts/latest')) return Promise.reject(new Error('v2 unavailable'))
      return Promise.reject(new Error(`unexpected request ${url}`))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderRoute()

    expect(await screen.findByText('v2 封签暂不可用')).toBeInTheDocument()
    expect(screen.getByText('读取失败；页面已停止展示旧版日期，避免误判为当前运行。')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/api/prediction-status'))).toBe(false)
  })

  it('renders the five scorecard axes without a best-agent ranking', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation((input) => {
      const url = String(input)
      if (url.endsWith('/api/v2/agent-scorecards')) return response({
        program_hash: program.program_hash,
        generated_at: '2026-08-12T20:00:00+08:00',
        sections: [{
          axis: 'final_system', title: '最终系统', items: [{
            agent_id: 'cio_agent', agent_name: 'CIO', target_id: 'csi1000-absolute-d1',
            signal_kind: 'decision_forecast', horizon: 'D1', sample_size: 20,
            independent_episodes: 20, average_brier: 0.19, baseline_brier: 0.22,
            brier_skill: 0.136, classwise_ece: { up: 0.03, neutral: 0.04, down: 0.05 }, direction_accuracy: 0.6,
            reasoning_average: 8.2, ablation_brier_delta: null, note: '正式前瞻样本',
          }],
        }],
        premarket_history: [{
          forecast_hash: 'f'.repeat(64), forecast_session: '2026-08-12', target_session: '2026-08-13',
          predicted_direction: 'up', realized_return: 0.01, actual_label: 'up', direction_correct: true,
          cumulative_sample_size: 1, cumulative_hits: 1, cumulative_win_rate: 1, rolling_20_win_rate: 1,
          long_only_period_return: 0.01, long_short_period_return: 0.01,
          long_only_cumulative_return: 0.01, long_short_cumulative_return: 0.01,
        }, {
          forecast_hash: 'e'.repeat(64), forecast_session: '2026-08-13', target_session: '2026-08-14',
          predicted_direction: 'down', realized_return: -0.02, actual_label: 'down', direction_correct: true,
          cumulative_sample_size: 2, cumulative_hits: 2, cumulative_win_rate: 1, rolling_20_win_rate: 1,
          long_only_period_return: 0, long_short_period_return: 0.02,
          long_only_cumulative_return: 0.01, long_short_cumulative_return: 0.0302,
        }],
      })
      if (url.endsWith('/api/v2/reasoning-reviews')) return response({ items: [{
        id: 'review-1', signal_id: 'signal-1', agent_id: 'cio_agent', target_id: 'csi1000-absolute-d1',
        status: 'completed', total_score: 8, human_review_required: true,
        human_review_status: 'pending', created_at: '2026-08-12T16:45:00+08:00',
      }] })
      return Promise.reject(new Error(`unexpected request ${url}`))
    }))

    renderRoute('/scorecards')

    expect(await screen.findByRole('heading', { name: 'Agent 分轴成绩单' })).toBeInTheDocument()
    for (const label of ['最终系统', '自然周期', 'D1 边际影响', '推理质量', '增量贡献']) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0)
    }
    expect(screen.getByText('13.6%')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '盘前预测历史成绩' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '策略收益曲线' })).toBeInTheDocument()
    expect(screen.getByText('+3.0%')).toBeInTheDocument()
    expect(screen.queryByText('当前最佳角色')).not.toBeInTheDocument()
  })

  it('renders Risk Critic coverage and missed-risk diagnostics without a direction score', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation((input) => {
      const url = String(input)
      if (url.endsWith('/api/v2/agent-scorecards')) return response({
        program_hash: program.program_hash,
        generated_at: '2026-08-12T20:00:00+08:00',
        sections: [{
          axis: 'reasoning', title: '推理质量', items: [{
            agent_id: 'risk_critic_agent', agent_name: 'Risk Critic', target_id: 'csi1000-absolute-d1',
            signal_kind: 'risk_critique', horizon: 'D1', sample_size: 0,
            independent_episodes: 0, average_brier: null, baseline_brier: null,
            brier_skill: null, classwise_ece: null, direction_accuracy: null,
            reasoning_average: 8, ablation_brier_delta: null, note: '不投方向票',
            risk_diagnostics: {
              critique_count: 20, counter_evidence_coverage_rate: 0.9,
              invalidation_coverage_rate: 0.8, risk_flag_rate: 0.35,
              evaluated_system_errors: 5, missed_risk_count: 2, missed_risk_rate: 0.4,
            },
          }],
        }],
      })
      if (url.endsWith('/api/v2/reasoning-reviews')) return response({ items: [] })
      return Promise.reject(new Error(`unexpected request ${url}`))
    }))

    renderRoute('/scorecards')

    expect(await screen.findByText('风险覆盖 90%')).toBeInTheDocument()
    expect(screen.getByText('失效条件 80%')).toBeInTheDocument()
    expect(screen.getByText('漏报 2 / 5 次系统错误')).toBeInTheDocument()
    expect(screen.queryByText('方向 0%')).not.toBeInTheDocument()
  })

  it('shows awaiting-draft state and target-specific release gates', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation((input) => {
      const url = String(input)
      if (url.endsWith('/api/agent-evals/suites')) return response({ items: [] })
      if (url.endsWith('/api/agent-bad-cases')) return response({ items: [] })
      if (url.endsWith('/api/agent-evals/jobs-v2')) return response({ items: [] })
      if (url.endsWith('/api/agent-evals/experiments')) return response({ items: [{
        id: 'eval-v2', suite_id: 'private-replay-v2', suite_version: '2.0.0', suite_hash: 'a'.repeat(64),
        baseline_target_id: 'baseline', baseline_target_hash: 'b'.repeat(64), candidate_target_id: 'candidate',
        candidate_target_hash: 'c'.repeat(64), status: 'awaiting_draft', release_decision: 'pending',
        policy_version: '2.0.0', created_at: '2026-08-12T18:00:00+08:00', started_at: null,
        completed_at: null, report_hash: null, error: null, result_count: 0, results: [],
        summary: { pending_arms: ['baseline', 'candidate'], targets: {
          'csi1000-absolute-d1': {
            decision: 'insufficient_sample', episode_count: 12,
            hard_gates: {
              schema_valid: true, cutoff_valid: true, citation_valid: true, trace_valid: true,
              must_pass_bad_case: { rate: 1, passed: true },
            },
            metric_gates: { brier_delta: 0.004, direction_drop: 0.01, p95_latency_ratio: 1.08, token_ratio: 1.05, passed: true },
            ablation: [{ agent_id: 'macro_agent', brier_delta: 0.01 }], reasoning: { candidate: { average: 8 } },
          },
        } },
      }] })
      return Promise.reject(new Error(`unexpected request ${url}`))
    }))

    renderRoute('/evaluations')

    expect(await screen.findByText('评测正在等待 Codex 草稿')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '按目标发布门禁' })).toBeInTheDocument()
    expect(screen.getByText('csi1000-absolute-d1')).toBeInTheDocument()
    expect(screen.getByText('5/5')).toBeInTheDocument()
  })
})
