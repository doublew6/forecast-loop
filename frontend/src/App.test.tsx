import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'

import { App } from './App'

function renderApp(route = '/') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function jsonResponse(body: unknown, status = 200, statusText = 'OK') {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status,
    statusText,
    headers: { 'Content-Type': 'application/json' },
  }))
}

const liveLatest = {
  run_id: 'RUN-LIVE-42',
  as_of: '2026-07-13T15:30:00+08:00',
  data_cutoff: '2026-07-13T15:00:00+08:00',
  forecasts: [],
}

const completedPredictionStatus = {
  today: {
    base_session: '2026-07-13',
    state: 'completed',
    attempted_at: '2026-07-13T16:10:00+08:00',
    attempt_status: 'prepared',
    run_id: 'RUN-LIVE-42',
    run_status: 'completed',
    message: '今日 Live 预测已完成并发布。',
  },
  latest_completed_run_id: 'RUN-LIVE-42',
  latest_completed_as_of: liveLatest.as_of,
  latest_completed_data_cutoff: liveLatest.data_cutoff,
  history: [],
}

function liveOpinion(id: string, horizon: 'D1' | 'D2') {
  return {
    id,
    agent_id: 'macro_policy_agent',
    agent_name: '宏观政策研究员',
    role: '宏观、流动性与政策传导',
    status: 'active',
    index_code: '000300.SH',
    horizon,
    direction: 'up',
    probabilities: { up: 0.21, neutral: 0.6, down: 0.19 },
    summary: '同一 Agent 在不同周期的意见。',
    evidence: ['政策保持稳定'],
    counter_evidence: [],
    invalidation_conditions: ['政策变化'],
    citations: [],
    contribution: '宏观输入',
    weight: 1,
  }
}

describe('forecast-loop app', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation((input) => {
      if (String(input).endsWith('/api/health')) {
        return jsonResponse({ mode: 'live' })
      }
      if (String(input).endsWith('/api/prediction-status')) {
        return jsonResponse(completedPredictionStatus)
      }
      return Promise.reject(new Error('backend offline'))
    }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the forecast-loop brand and F mark', async () => {
    const { container } = renderApp()

    expect(await screen.findAllByText('forecast-loop')).not.toHaveLength(0)
    expect(container.querySelector('.brand-name')).toHaveTextContent('forecast-loop')
    expect(container.querySelector('.brand-subtitle')).toHaveTextContent('可验证预测 Agent 框架')
    expect(container.querySelector('.brand-mark')).toHaveTextContent('F')
    expect(container.querySelector('.mobile-brand-mark')).toHaveTextContent('F')
    expect(screen.queryByText('SignalRace')).not.toBeInTheDocument()
    expect(screen.queryByText('VeriCouncil')).not.toBeInTheDocument()
  })

  it('falls back explicitly and renders all five broad indexes', async () => {
    renderApp()

    expect(await screen.findByRole('heading', { name: '今日投委会决策' })).toBeInTheDocument()
    expect(await screen.findByTestId('demo-banner')).toHaveTextContent('后端接口不可用')
    expect(screen.getByRole('heading', { name: '沪深300' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '中证500' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '中证1000' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '创业板指' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '科创50' })).toBeInTheDocument()
  })

  it('renders configured single-equity forecasts without fixed A-share tabs', async () => {
    const appleForecast = {
      id: 'FORECAST-AAPL-D2',
      index_code: 'AAPL.US',
      index_name: 'Apple',
      horizon: 'D2',
      target_date: '2026-07-14',
      direction: 'up',
      probabilities: { up: 0.48, neutral: 0.34, down: 0.18 },
      threshold: 0.006,
      confidence: 0.73,
      rationale: '版本化单股 Universe 的动态预测。',
      citations: [],
    }
    vi.mocked(fetch).mockImplementation((input) => {
      const path = String(input)
      if (path.endsWith('/api/health')) return jsonResponse({ mode: 'live' })
      if (path.endsWith('/api/prediction-status')) return jsonResponse(completedPredictionStatus)
      if (path.endsWith('/api/forecasts/latest')) {
        return jsonResponse({
          run_id: 'RUN-US-1',
          as_of: '2026-07-10T16:00:00-04:00',
          data_cutoff: '2026-07-10T16:00:00-04:00',
          forecasts: [appleForecast],
        })
      }
      if (path.endsWith('/api/market-universe')) {
        return jsonResponse({
          schema_version: 'forecast-loop.market-universe/v1',
          universe_id: 'single-stock-aapl',
          version: '1.0.0',
          market: 'US',
          timezone: 'America/New_York',
          calendar_id: 'XNAS',
          session_close: '16:00',
          horizons: ['D1', 'D2'],
          instruments: [{
            code: 'AAPL.US',
            name: 'Apple',
            asset_type: 'equity',
            exchange: 'XNAS',
            currency: 'USD',
            sector: 'Information Technology',
            strategy_bucket: 'growth',
            tags: ['single-stock'],
            wiki_entry_ids: {},
            agent_briefs: {},
          }],
          content_hash: 'a'.repeat(64),
        })
      }
      if (path.endsWith('/api/meetings/RUN-US-1')) {
        return jsonResponse({
          run: {
            id: 'RUN-US-1',
            as_of: '2026-07-10T16:00:00-04:00',
            data_cutoff: '2026-07-10T16:00:00-04:00',
            status: 'completed',
            mode: 'live',
            data_quality: {},
          },
          opinions: [],
          forecasts: [appleForecast],
          workflow_steps: [],
        })
      }
      return jsonResponse({}, 404, 'Not Found')
    })

    renderApp()

    expect(await screen.findByRole('heading', { name: 'Apple' })).toBeInTheDocument()
    expect(screen.getByText('AAPL.US')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '沪深300' })).not.toBeInTheDocument()
  })

  it('switches between D2 and D1 forecasts', async () => {
    const user = userEvent.setup()
    renderApp()

    await screen.findByRole('heading', { name: '今日投委会决策' })
    expect(screen.getByText('未来两交易日方向预测')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /D1/ }))
    expect(screen.getByText('下一交易日方向预测')).toBeInTheDocument()
  })

  it('renders the Wiki verification chain in fallback mode', async () => {
    renderApp('/wiki')

    expect(await screen.findByRole('heading', { name: '可验证知识库' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'AI 存储产业链领先指标' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '原始资料' })).toBeInTheDocument()
  })

  it('seals a User Agent judgment before revealing the committee direction', async () => {
    const target = {
      forecast_id: 'FORECAST-USER-1',
      run_id: 'RUN-LIVE-42',
      mode: 'live',
      index_code: '000300.SH',
      index_name: '沪深300',
      horizon: 'D2',
      base_trade_date: '2026-07-25',
      target_date: '2026-07-29',
      as_of: '2026-07-25T15:00:00+08:00',
      data_cutoff: '2026-07-25T15:00:00+08:00',
      submission_deadline: '2026-07-25T22:30:00+08:00',
      submission_open: true,
      submission_note: '声明盲判后可进入用户影子成绩。',
      score_eligible_if_blind: true,
      existing_judgment_id: null,
    }
    let submitted: Record<string, unknown> | undefined
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = String(input)
      if (path.endsWith('/api/health')) return jsonResponse({ mode: 'live' })
      if (path.endsWith('/api/user-judgments/targets')) return jsonResponse({ items: [target] })
      if (path.endsWith('/api/user-judgments') && init?.method !== 'POST') {
        return jsonResponse({ items: [] })
      }
      if (path.endsWith('/api/user-judgments') && init?.method === 'POST') {
        submitted = JSON.parse(String(init.body)) as Record<string, unknown>
        return jsonResponse({
          id: 'JUDGMENT-1',
          actor_id: 'local-operator',
          agent_id: 'user_judgment_agent',
          agent_version: '0.1.0',
          forecast_id: target.forecast_id,
          run_id: target.run_id,
          mode: 'live',
          index_code: target.index_code,
          index_name: target.index_name,
          horizon: target.horizon,
          target_date: target.target_date,
          direction: 'up',
          confidence: 0.6,
          rationale: '流动性改善与风险偏好回升可能共同推动目标指数在预测窗口内走强。',
          counter_evidence: '海外利率重新上行可能压制成长估值，并削弱流动性改善效果。',
          invalidation_condition: '若成交额显著收缩且跌破基准日低点，则本判断失效。',
          blind_attestation: true,
          submitted_at: '2026-07-25T20:31:00+08:00',
          submission_deadline: target.submission_deadline,
          formal_score_eligible: true,
          run_input_hash: 'a'.repeat(64),
          forecast_input_hash: 'b'.repeat(64),
          policy_version: 'user-judgment/v1',
          content_hash: 'c'.repeat(64),
          wiki_path: 'decisions/2026-07-29/JUDGMENT-1.md',
          wiki_artifact_hash: 'd'.repeat(64),
          wiki_url: '/api/user-judgments/JUDGMENT-1/wiki',
          committee_direction: 'down',
          committee_agreement: false,
          evaluation: null,
        }, 201, 'Created')
      }
      return jsonResponse({}, 404, 'Not Found')
    })

    const user = userEvent.setup()
    renderApp('/judgments')

    expect(await screen.findByRole('heading', { name: '我的独立判断' })).toBeInTheDocument()
    expect(screen.queryByText('封签后揭示委员会判断')).not.toBeInTheDocument()
    await user.click(screen.getByRole('radio', { name: /上涨/ }))
    await user.type(
      screen.getByLabelText(/我的核心理由/),
      '流动性改善与风险偏好回升可能共同推动目标指数在预测窗口内走强。',
    )
    await user.type(
      screen.getByLabelText(/最强反方证据/),
      '海外利率重新上行可能压制成长估值，并削弱流动性改善效果。',
    )
    await user.type(
      screen.getByLabelText(/什么情况会证明我错了/),
      '若成交额显著收缩且跌破基准日低点，则本判断失效。',
    )
    await user.click(screen.getByRole('checkbox', { name: /尚未查看/ }))
    await user.click(screen.getByRole('button', { name: '冻结我的判断' }))

    expect(await screen.findByText('封签后揭示委员会判断')).toBeInTheDocument()
    expect(screen.getByText('分歧')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /查看 Wiki 快照/ })).toHaveAttribute(
      'href',
      '/api/user-judgments/JUDGMENT-1/wiki',
    )
    expect(submitted).toMatchObject({
      forecast_id: target.forecast_id,
      direction: 'up',
      blind_attestation: true,
    })
  })

  it('keeps a failed judgment ledger distinct from an empty one and selects the available target', async () => {
    vi.mocked(fetch).mockImplementation((input) => {
      const path = String(input)
      if (path.endsWith('/api/health')) return jsonResponse({ mode: 'live' })
      if (path.endsWith('/api/user-judgments/targets')) {
        return jsonResponse({
          items: [{
            forecast_id: 'FORECAST-ONLY-D1',
            run_id: 'RUN-LIVE-43',
            mode: 'live',
            index_code: '000905.SH',
            index_name: '中证500',
            horizon: 'D1',
            base_trade_date: '2026-07-25',
            target_date: '2026-07-28',
            as_of: '2026-07-25T15:00:00+08:00',
            data_cutoff: '2026-07-25T15:00:00+08:00',
            submission_deadline: '2026-07-25T22:30:00+08:00',
            submission_open: true,
            submission_note: '判断窗口开放。',
            score_eligible_if_blind: true,
            existing_judgment_id: null,
          }],
        })
      }
      if (path.endsWith('/api/user-judgments')) {
        return jsonResponse({ detail: 'ledger unavailable' }, 503, 'Unavailable')
      }
      return jsonResponse({}, 404, 'Not Found')
    })

    renderApp('/judgments')

    expect(await screen.findByRole('button', { name: /中证500/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(await screen.findByRole('alert')).toHaveTextContent('无法读取封签账本')
    expect(screen.queryByText('还没有个人判断')).not.toBeInTheDocument()
  })

  it('resolves /meeting from latest and counts unique active agents', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation((input) => {
      const path = String(input)
      if (path.endsWith('/api/health')) return jsonResponse({ mode: 'live' })
      if (path.endsWith('/api/forecasts/latest')) return jsonResponse(liveLatest)
      if (path.endsWith('/api/meetings/RUN-LIVE-42')) {
        return jsonResponse({
          run: {
            id: 'RUN-LIVE-42',
            as_of: liveLatest.as_of,
            data_cutoff: liveLatest.data_cutoff,
            status: 'completed',
            mode: 'live',
            data_quality: {},
          },
          opinions: [liveOpinion('macro-d1', 'D1'), liveOpinion('macro-d2', 'D2')],
          forecasts: [],
          workflow_steps: [],
        })
      }
      return jsonResponse({}, 404, 'Not Found')
    })

    renderApp('/meeting')

    expect(await screen.findByRole('heading', { name: '投委会决策详情' })).toBeInTheDocument()
    expect(screen.getByText('1 个 Agent')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /查看到期反省/ })).toHaveAttribute(
      'href',
      '/reflections?run=RUN-LIVE-42',
    )
    const requestedPaths = fetchMock.mock.calls.map(([input]) => String(input))
    expect(requestedPaths).toContain('/api/meetings/RUN-LIVE-42')
    expect(requestedPaths.some((path) => path.includes('RUN-20260713-001'))).toBe(false)
  })

  it('does not mix static opinions into a live latest run when its meeting fails', async () => {
    vi.mocked(fetch).mockImplementation((input) => {
      const path = String(input)
      if (path.endsWith('/api/health')) return jsonResponse({ mode: 'live' })
      if (path.endsWith('/api/forecasts/latest')) return jsonResponse(liveLatest)
      if (path.endsWith('/api/prediction-status')) return jsonResponse(completedPredictionStatus)
      if (path.endsWith('/api/meetings/RUN-LIVE-42')) return jsonResponse({}, 503, 'Service Unavailable')
      return jsonResponse({}, 404, 'Not Found')
    })

    renderApp('/')

    expect(await screen.findByRole('alert')).toHaveTextContent('运行 RUN-LIVE-42 的会议详情加载失败')
    expect(screen.getByText('委员意见不可用')).toBeInTheDocument()
    expect(screen.queryByText('国内政策预期维持托底，但海外利率与人民币波动尚未给出单边确认。')).not.toBeInTheDocument()
    expect(screen.queryByTestId('demo-banner')).not.toBeInTheDocument()
  })

  it('labels old forecasts as the latest trading-day result when today is blocked', async () => {
    vi.mocked(fetch).mockImplementation((input) => {
      const path = String(input)
      if (path.endsWith('/api/health')) return jsonResponse({ mode: 'live' })
      if (path.endsWith('/api/forecasts/latest')) return jsonResponse(liveLatest)
      if (path.endsWith('/api/prediction-status')) {
        return jsonResponse({
          ...completedPredictionStatus,
          today: {
            base_session: '2026-07-14',
            state: 'blocked',
            attempted_at: '2026-07-14T16:10:00+08:00',
            attempt_status: 'blocked_upstream',
            message: '上游数据质量闸门未通过。',
          },
          history: [{
            attempt_id: 'ATTEMPT-BLOCKED',
            base_session: '2026-07-14',
            attempted_at: '2026-07-14T16:10:00+08:00',
            status: 'blocked_upstream',
            state: 'blocked',
            error_code: 'quality_gate_failed',
            message: '上游数据质量闸门未通过。',
            receipt_hash: 'a'.repeat(64),
          }],
        })
      }
      if (path.endsWith('/api/meetings/RUN-LIVE-42')) {
        return jsonResponse({
          run: {
            id: 'RUN-LIVE-42',
            as_of: liveLatest.as_of,
            data_cutoff: liveLatest.data_cutoff,
            status: 'completed',
            mode: 'live',
            data_quality: {},
          },
          opinions: [],
          forecasts: [],
          workflow_steps: [],
        })
      }
      return jsonResponse({}, 404, 'Not Found')
    })

    renderApp('/')

    expect(await screen.findByRole('heading', { name: '最近交易日投委会决策' })).toBeInTheDocument()
    expect(screen.getByTestId('prediction-daily-status')).toHaveTextContent(
      '今日预测已阻断：上游数据质量闸门未通过。',
    )
    expect(screen.getByRole('heading', { name: '每日运行记录' })).toBeInTheDocument()
    expect(screen.getByText('阻断')).toBeInTheDocument()
  })

  it('keeps a previous incomplete formal run visible as overdue after midnight', async () => {
    vi.mocked(fetch).mockImplementation((input) => {
      const path = String(input)
      if (path.endsWith('/api/health')) return jsonResponse({ mode: 'live' })
      if (path.endsWith('/api/forecasts/latest')) return jsonResponse(liveLatest)
      if (path.endsWith('/api/prediction-status')) {
        return jsonResponse({
          ...completedPredictionStatus,
          today: {
            base_session: '2026-07-14',
            state: 'pending',
            message: '今日确定性准备任务将在配置的时间运行。',
          },
          history: [{
            attempt_id: 'ATTEMPT-OVERDUE',
            base_session: '2026-07-13',
            attempted_at: '2026-07-13T16:20:00+08:00',
            status: 'already_prepared',
            state: 'overdue',
            run_id: 'RUN-LIVE-42',
            run_status: 'awaiting_draft',
            message: '该日 Live 预测未在配置的运行时限内完成。',
            receipt_hash: 'b'.repeat(64),
          }],
        })
      }
      if (path.endsWith('/api/meetings/RUN-LIVE-42')) {
        return jsonResponse({
          run: {
            id: 'RUN-LIVE-42',
            as_of: liveLatest.as_of,
            data_cutoff: liveLatest.data_cutoff,
            status: 'completed',
            mode: 'live',
            data_quality: {},
          },
          opinions: [],
          forecasts: [],
          workflow_steps: [],
        })
      }
      return jsonResponse({}, 404, 'Not Found')
    })

    renderApp('/')

    expect(await screen.findByRole('heading', { name: '每日运行记录' })).toBeInTheDocument()
    expect(screen.getByText('2026-07-13')).toBeInTheDocument()
    expect(screen.getByText('逾期')).toBeInTheDocument()
    expect(screen.getByText('该日 Live 预测未在配置的运行时限内完成。')).toBeInTheDocument()
  })

  it('selects and highlights the Wiki entry section encoded in the URL', async () => {
    renderApp('/wiki?entry=WIKI-IDX-EXPOSURE-001&section=growth')

    expect(await screen.findByRole('heading', { name: '五个宽基指数暴露地图' })).toBeInTheDocument()
    const section = screen.getByRole('heading', { name: /§4 成长指数/ }).closest('article')
    expect(section).toHaveClass('citation-target')
  })

  it('keeps evaluation disabled until observations are imported', async () => {
    const fetchMock = vi.mocked(fetch)
    renderApp('/runs')

    const evaluate = await screen.findByRole('button', { name: '运行到期评分' })
    expect(evaluate).toBeDisabled()
    expect(screen.getByText('需先导入到期行情，当前不会提交空评分。')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/api/evaluations/run'))).toBe(false)
  })

  it('renders evidence-bound reflection findings, outcomes, and lesson status', async () => {
    vi.mocked(fetch).mockImplementation((input) => {
      const path = String(input)
      if (path.endsWith('/api/health')) return jsonResponse({ mode: 'live' })
      if (path.endsWith('/api/reflections')) {
        return jsonResponse({
          items: [{
            id: 'REF-EXTREME-1',
            source_run_id: 'RUN-LIVE-42',
            target_date: '2026-07-16',
            horizon: 'D1',
            status: 'completed',
            supersedes_id: 'REF-EXTREME-0',
            overall_severity: 'systemic_extreme_down',
            metrics: {
              outcome_count: 5,
              sign_sample_size: 5,
              sign_correct: 0,
              sign_accuracy: 0,
              material_sample_size: 5,
              material_correct: 0,
              material_direction_accuracy: 0,
              average_brier: 0.61,
            },
          }],
        })
      }
      if (path.endsWith('/api/reflections/REF-EXTREME-1')) {
        const marketFinding = {
          id: 'FIND-MARKET',
          scope_type: 'market_event',
          subject_id: 'broad-market',
          index_code: '000300.SH',
          horizon: 'D1',
          verdict: 'wrong',
          primary_error_type: 'post_cutoff_shock',
          secondary_error_types: [],
          evidence_ids: ['PRICE-20260716'],
          availability_class: 'post_cutoff_event',
          causal_status: 'verified',
          confidence: 0.96,
          summary: '五个宽基指数同步下跌，达到预设极端冲击门槛。',
          counterfactual: { direction: 'up', would_flip: false, causal_label: 'verified' },
          remediation: [],
        }
        const agentFinding = {
          id: 'FIND-AGENT',
          scope_type: 'agent',
          subject_id: 'macro_policy_agent',
          index_code: '000300.SH',
          horizon: 'D1',
          verdict: 'wrong',
          primary_error_type: 'attention_omission',
          secondary_error_types: ['reasoning_or_weighting_failure'],
          evidence_ids: ['EVIDENCE-USED'],
          availability_class: 'coverage_gap_pre_cutoff',
          causal_status: 'supported',
          confidence: 0.84,
          summary: '截止前已有流动性压力线索，但未被纳入宏观判断。',
          what_was_right: [],
          what_was_wrong: ['低估了截止前已经冻结的流动性压力。'],
          original_evidence_item_ids: ['EVIDENCE-USED'],
          missed_evidence_item_ids: ['EVIDENCE-MISSED'],
          source_ids: ['SOURCE-LIQUIDITY'],
          invalidation_conditions_triggered: ['流动性压力失效门槛已触发。'],
          counterfactual: {
            direction: 'down',
            probabilities: { up: 0.18, neutral: 0.12, down: 0.7 },
            would_flip: true,
            causal_label: 'supported',
            basis: 'leave_one_input_out',
            explanation: '移除被高估的单项利多后，方向敏感地翻转为下跌。',
          },
          remediation: ['补充流动性冲击检查项'],
          lesson_candidate_ids: ['LESSON-LIQUIDITY'],
        }
        const committeeFinding = {
          id: 'FIND-COMMITTEE',
          scope_type: 'committee',
          subject_id: 'cio_agent',
          horizon: 'D1',
          verdict: 'unresolved',
          primary_error_type: 'unresolved',
          secondary_error_types: [],
          evidence_ids: [],
          availability_class: 'after_close_explanation',
          causal_status: 'hypothesis',
          confidence: 0.45,
          summary: '收盘后的新闻解释只能作为假设，不能回填昨日证据。',
          counterfactual: {},
          remediation: [],
        }
        return jsonResponse({
          id: 'REF-EXTREME-1',
          source_run_id: 'RUN-LIVE-42',
          target_date: '2026-07-16',
          horizon: 'D1',
          status: 'completed',
          supersedes_id: 'REF-EXTREME-0',
          overall_severity: 'systemic_extreme_down',
          completed_at: '2026-07-16T16:20:00+08:00',
          prediction_cutoff: '2026-07-15T15:00:00+08:00',
          reflection_cutoff: '2026-07-16T16:00:00+08:00',
          summary: '上涨判断遭遇系统性下跌。',
          lesson_candidate_ids: ['LESSON-LIQUIDITY'],
          metrics: {
            outcome_count: 1,
            sign_sample_size: 1,
            sign_correct: 0,
            sign_accuracy: 0,
            material_sample_size: 1,
            material_correct: 0,
            material_direction_accuracy: 0,
            average_brier: 0.61,
          },
          source_timeline: [{
            id: 'SOURCE-LIQUIDITY',
            title: '盘中流动性压力确认',
            summary: '该来源发布于预测截止后、目标收盘前。',
            source_url: 'https://example.com/liquidity',
            event_time: '2026-07-16T10:10:00+08:00',
            published_at: '2026-07-16T10:15:00+08:00',
            ingested_at: '2026-07-16T16:05:00+08:00',
            source_kind: 'official_release',
            related_index_codes: ['000300.SH'],
            time_class: 'post_cutoff_preclose',
            content_hash: 'sha256:source-liquidity',
          }],
          outcomes: [{
            forecast_id: 'FORECAST-300-D1',
            index_code: '000300.SH',
            index_name: '沪深300',
            horizon: 'D1',
            predicted_direction: 'up',
            actual_label: 'down',
            market_snapshot: {
              actual_return: -0.045,
              amount: 1_200_000,
              advancers: 420,
              decliners: 4720,
              unchanged: 60,
              limit_down_count: 96,
              breadth_down_ratio: 0.9077,
              sector_contributions: [{ name: '券商', contribution: -0.012 }],
              weight_contributions: [{ name: '贵州茅台', contribution: -0.006 }],
              history_sample_size: 120,
            },
            diagnostic: {
              signed_sigma: -3.21,
              severity: 'extreme',
              systemic_extreme_down: true,
              material_direction_correct: false,
              brier_score: 0.61,
              history_sample_size: 120,
              data_incomplete: true,
              policy_version: 'reflection-v1',
            },
          }],
          findings: [marketFinding, agentFinding, committeeFinding],
          decision_chain: [agentFinding, committeeFinding],
          lesson_proposals: [{
            id: 'LESSON-LIQUIDITY',
            reflection_run_id: 'REF-EXTREME-1',
            title: '流动性冲击必须进入截止前检查',
            summary: '连续案例确认前仅保留为候选经验。',
            status: 'candidate',
            independent_episode_count: 1,
            half_life_sessions: 60,
            replay_target_dates: 0,
          }],
        })
      }
      if (path.endsWith('/api/lessons')) {
        return jsonResponse({
          items: [{
            id: 'LESSON-LIQUIDITY',
            reflection_run_id: 'REF-EXTREME-1',
            title: '流动性冲击必须进入截止前检查',
            summary: '连续案例确认前仅保留为候选经验。',
            status: 'active',
            independent_episode_count: 5,
            half_life_sessions: 60,
            replay_target_dates: 20,
            replay_batch_count: 2,
            latest_replay_hash: 'sha256:1234567890abcdef1234567890abcdef',
            supersedes_id: 'LESSON-LIQUIDITY-V0',
            revalidation_due: true,
            revalidation_due_reasons: ['monthly', 'new_20_target_dates'],
            replay_metrics: {
              wiki_review_ready: false,
              wiki_promotion_status: 'not_promoted',
              blockers: ['calibration_not_improved'],
            },
            lifecycle_history: [{
              id: 'LESSON-EVENT-1',
              event_type: 'replay_recorded',
              from_status: 'candidate',
              to_status: 'candidate',
              actor: 'reflection-replay',
              reason: 'deterministic replay observations recorded',
              payload_hash: 'sha256:event-1',
              occurred_at: '2026-07-16T16:25:00+08:00',
            }, {
              id: 'LESSON-EVENT-2',
              event_type: 'approved',
              from_status: 'candidate',
              to_status: 'active',
              actor: 'lesson-reviewer',
              reason: 'Replay evidence checked by a human reviewer.',
              payload_hash: 'sha256:event-2',
              occurred_at: '2026-07-16T16:30:00+08:00',
            }],
          }],
        })
      }
      if (path.endsWith('/api/meetings/RUN-LIVE-42')) {
        return jsonResponse({
          run: {
            id: 'RUN-LIVE-42',
            as_of: liveLatest.as_of,
            data_cutoff: liveLatest.data_cutoff,
            status: 'completed',
            mode: 'live',
            data_quality: {},
          },
          opinions: [liveOpinion('macro-d1', 'D1')],
          forecasts: [],
          workflow_steps: [
            { id: 'evidence', label: '事前证据冻结', status: 'completed' },
            { id: 'research', label: '研究 Agent 分析', status: 'completed' },
            { id: 'strategy', label: '策略研究员合成', status: 'completed' },
            { id: 'risk', label: 'Risk Critic 审查', status: 'completed' },
            { id: 'cio', label: 'CIO 最终裁决', status: 'completed' },
          ],
        })
      }
      return jsonResponse({}, 404, 'Not Found')
    })

    renderApp('/reflections')

    expect(await screen.findByRole('heading', { name: '每日反省' })).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: '预测 / 实际矩阵' })).toBeInTheDocument()
    expect(screen.getAllByText('极端冲击').length).toBeGreaterThan(0)
    expect(screen.getAllByText('沪深300').length).toBeGreaterThan(0)
    expect(screen.getByText('-4.50%')).toBeInTheDocument()
    expect(screen.getByText('-3.21σ')).toBeInTheDocument()
    expect(screen.getByText('D1 符号命中率')).toBeInTheDocument()
    expect(screen.getByText('D1 重大行情命中率')).toBeInTheDocument()
    expect(screen.getByText('D1 三分类 Brier')).toBeInTheDocument()
    expect(screen.getByText('0/1 命中')).toBeInTheDocument()
    expect(screen.getByText('0/1 命中 · 仅统计噪声带外')).toBeInTheDocument()
    expect(screen.getByText('0.610')).toBeInTheDocument()
    expect(await screen.findByText('CIO 最终裁决')).toBeInTheDocument()
    expect(screen.getAllByText('反事实敏感度').length).toBeGreaterThan(0)
    expect(screen.getByText('移除单项输入')).toBeInTheDocument()
    expect(screen.getByText('下跌 70%')).toBeInTheDocument()
    expect(screen.getByText('移除被高估的单项利多后，方向敏感地翻转为下跌。')).toBeInTheDocument()
    expect(screen.getByText('低估了截止前已经冻结的流动性压力。')).toBeInTheDocument()
    expect(screen.getByText('流动性压力失效门槛已触发。')).toBeInTheDocument()
    expect(screen.getByText('EVIDENCE-USED')).toBeInTheDocument()
    expect(screen.getByText('EVIDENCE-MISSED')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'SOURCE-LIQUIDITY' })).toHaveAttribute(
      'href',
      '#reflection-source-SOURCE-LIQUIDITY',
    )
    expect(screen.getByRole('heading', { name: '收盘宽度与贡献' })).toBeInTheDocument()
    expect(screen.getByText('91%')).toBeInTheDocument()
    expect(screen.getByText('420 / 4720 / 60')).toBeInTheDocument()
    expect(screen.getByText('96')).toBeInTheDocument()
    expect(screen.getByText('120 日')).toBeInTheDocument()
    expect(screen.getByText('data_incomplete')).toBeInTheDocument()
    expect(screen.getByText('券商 -1.20%')).toBeInTheDocument()
    expect(screen.getByText('贵州茅台 -0.60%')).toBeInTheDocument()
    expect(screen.getAllByText(/修订自 REF-EXTREME-0/).length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('Quant 占位 Agent：不适用')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '事后来源时间线' })).toBeInTheDocument()
    expect(screen.getByText('盘中流动性压力确认')).toBeInTheDocument()
    expect(screen.getByText('截止后 · 收盘前')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /查看来源/ })).toHaveAttribute(
      'href',
      'https://example.com/liquidity',
    )
    expect(screen.getAllByText('截止前覆盖缺口').length).toBeGreaterThan(0)
    expect(screen.getAllByText('截止后新事件').length).toBeGreaterThan(0)
    expect(screen.getAllByText('收盘后解释').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/因果已验证/).length).toBeGreaterThan(0)
    expect(screen.getByText('有证据支持 · 84%')).toBeInTheDocument()
    expect(screen.getByText('流动性冲击必须进入截止前检查')).toBeInTheDocument()
    expect(screen.getByText('经验已激活')).toBeInTheDocument()
    expect(screen.getByText('经验激活 ≠ Wiki 晋升')).toBeInTheDocument()
    expect(screen.getByText('尚未晋升正式 Wiki')).toBeInTheDocument()
    expect(screen.getByText(/active 只表示经验生命周期已激活/)).toBeInTheDocument()
    expect(screen.getByText('重放日期')).toBeInTheDocument()
    expect(screen.getByText('重放批次')).toBeInTheDocument()
    expect(screen.getByText('Wiki 人工审查未就绪')).toBeInTheDocument()
    expect(screen.getByText('概率校准尚未改善')).toBeInTheDocument()
    expect(screen.getByText('需要重新验证')).toBeInTheDocument()
    expect(screen.getByText('月度复核到期 · 新增 20 个目标日期')).toBeInTheDocument()
    expect(screen.getByText('1234567890ab…')).toBeInTheDocument()
    expect(screen.getByText('LESSON-LIQUIDITY-V0')).toBeInTheDocument()
    expect(screen.getByText('最近生命周期事件')).toBeInTheDocument()
    expect(screen.getByText('人工激活')).toBeInTheDocument()
    expect(screen.getByText('候选经验 → 经验已激活')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'RUN-LIVE-42' })).toHaveAttribute(
      'href',
      '/meeting/RUN-LIVE-42',
    )
  })

  it('links scorecards to the reflection workbench', async () => {
    renderApp('/scorecards')

    expect(await screen.findByRole(
      'heading',
      { name: 'Agent 历史成绩单' },
      { timeout: 3_000 },
    )).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /查看每日反省/ })).toHaveAttribute('href', '/reflections')
    expect(screen.getByText('无正式 Live 样本')).toBeInTheDocument()
    expect(screen.getByText('无噪声带外样本')).toBeInTheDocument()
  })

  it('renders the three scorecard metrics and keeps legacy accuracy compatible', async () => {
    vi.mocked(fetch).mockImplementation((input) => {
      const path = String(input)
      if (path.endsWith('/api/health')) return jsonResponse({ mode: 'live' })
      if (path.endsWith('/api/agents')) {
        return jsonResponse({
          items: [
            {
              id: 'cio_agent',
              name: 'CIO 投委会',
              role: '最终组合判断',
              kind: 'committee',
              workflow_role: 'decision',
              source_type: 'deterministic',
              version: '1.0.0',
              weight: 1,
              status: 'active',
            },
            {
              id: 'strategy_agent',
              name: '市场策略研究员',
              role: '信息合成与最终方向',
              kind: 'strategy',
              workflow_role: 'strategy',
              source_type: 'ai',
              version: '1.0.0',
              weight: 1,
              status: 'active',
            },
          ],
        })
      }
      if (path.includes('/api/agents/cio_agent/scorecard')) {
        return jsonResponse({
          agent_id: 'cio_agent',
          sample_size: 10,
          sample_sufficient: true,
          accuracy: 0.8,
          sign_sample_size: 10,
          sign_correct: 8,
          sign_accuracy: 0.8,
          material_sample_size: 6,
          material_correct: 5,
          material_direction_accuracy: 5 / 6,
          average_brier: 0.142,
          direction_metrics: [],
          calibration: [],
          expected_calibration_error: null,
          agent_version: '1.0.0',
          model_name: 'codex-live',
          note: '正式 Live 评分。',
        })
      }
      if (path.includes('/api/agents/strategy_agent/scorecard')) {
        return jsonResponse({
          agent_id: 'strategy_agent',
          sample_size: 10,
          sample_sufficient: false,
          accuracy: 0.6,
          average_brier: 0.24,
          direction_metrics: [],
          calibration: [],
          expected_calibration_error: null,
          agent_version: '1.0.0',
          model_name: 'legacy-live',
          note: '旧后端兼容样本。',
        })
      }
      return jsonResponse({}, 404, 'Not Found')
    })

    renderApp('/scorecards')

    expect(await screen.findByText('投委会符号命中率')).toBeInTheDocument()
    expect(screen.getByText('投委会重大行情命中率')).toBeInTheDocument()
    expect(screen.getByText('投委会三分类 Brier')).toBeInTheDocument()
    expect(screen.getByText('8/10 命中')).toBeInTheDocument()
    expect(screen.getByText('5/6 命中 · 仅统计噪声带外')).toBeInTheDocument()
    expect(screen.getAllByText('0.142').length).toBeGreaterThan(0)
    expect(screen.getByRole('columnheader', { name: '符号命中率' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: '重大行情命中率' })).toBeInTheDocument()
    expect(screen.getByText('市场策略研究员')).toBeInTheDocument()
    expect(screen.getByText('Decision · 注册来源 Rule')).toBeInTheDocument()
    expect(screen.getByText('Strategy · 注册来源 AI')).toBeInTheDocument()
    expect(screen.getAllByText('60%').length).toBeGreaterThanOrEqual(2)
  })
})
