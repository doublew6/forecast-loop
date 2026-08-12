import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'

import { App } from './App'

function renderRoute(route: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function response(body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }))
}

const trace = {
  id: 'abc123trace',
  workflow_kind: 'agent_eval',
  subject_id: 'experiment-1',
  mode: 'offline',
  status: 'completed',
  started_at: '2026-08-07T18:00:00+08:00',
  completed_at: '2026-08-07T18:00:02+08:00',
  duration_ms: 2000,
  input_hash: 'a'.repeat(64),
  trace_policy_version: '1.0.0',
  telemetry_complete: true,
  error_code: null,
  error_summary: null,
  attributes: { experiment_id: 'experiment-1' },
  span_count: 1,
  spans: [],
  external_url: null,
  audit_url: null,
  audit_label: null,
}

describe('Agent operations pages', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('shows a release verdict and the bad-case governance queue', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation((input) => {
      const url = String(input)
      if (url.endsWith('/api/agent-evals/suites')) return response({ items: [{
        suite_id: 'agent-workflow-v1', version: '1.0.0', title: 'Agent Workflow Contract Benchmark',
        description: 'fixture', synthetic: true, runner_kind: 'fixture', case_count: 20,
        target_ids: ['baseline-v1', 'candidate-v2'], arm_ids: ['baseline-v1', 'candidate-v2'], content_hash: 'b'.repeat(64), source: 'public',
      }] })
      if (url.endsWith('/api/agent-evals/experiments')) return response({ items: [{
        id: 'experiment-1', suite_id: 'agent-workflow-v1', suite_version: '1.0.0',
        suite_hash: 'b'.repeat(64), baseline_target_id: 'baseline-v1', baseline_target_hash: 'c'.repeat(64),
        candidate_target_id: 'candidate-v2', candidate_target_hash: 'd'.repeat(64), status: 'completed',
        release_decision: 'pass', policy_version: '1.0.0', created_at: '2026-08-07T18:00:00+08:00',
        started_at: '2026-08-07T18:00:00+08:00', completed_at: '2026-08-07T18:00:02+08:00',
        report_hash: 'e'.repeat(64), error: null, result_count: 280, results: [],
        summary: { must_pass_rate: 1, metric_gates: { brier_delta: 0.005, p95_latency_ratio: 1.08, token_ratio: 1.09 } },
      }] })
      if (url.endsWith('/api/agent-bad-cases')) return response({ items: [] })
      return Promise.reject(new Error(`unexpected request ${url}`))
    }))

    renderRoute('/evaluations')
    expect(await screen.findByRole('heading', { name: 'Agent 版本放行台' })).toBeInTheDocument()
    expect(await screen.findAllByText('允许放行')).not.toHaveLength(0)
    expect(screen.getByRole('heading', { name: '待治理案例' })).toBeInTheDocument()
    expect(screen.getByText('当前没有待处理 bad case')).toBeInTheDocument()
  })

  it('links a prediction trace to audit detail and expands sanitized citations', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn().mockImplementation((input) => {
      const url = String(input)
      if (url.includes('/api/agent-observability/summary')) return response({
        window_hours: 24, total_traces: 1, running_traces: 0, completed_traces: 1,
        failed_traces: 0, degraded_traces: 0, telemetry_complete_rate: 1,
        completion_rate: 1, p95_duration_ms: 2000, by_workflow_kind: { agent_eval: 1 }, recent: [trace],
      })
      if (url.includes('/api/agent-traces') && !url.includes('/abc123trace')) {
        if (url.includes('cursor=next-page')) {
          return response({ items: [{ ...trace, id: 'second-trace', subject_id: 'experiment-2' }] })
        }
        return response({ items: [trace], next_cursor: 'next-page' })
      }
      if (url.endsWith('/api/agent-traces/abc123trace')) return response({
        ...trace,
        workflow_kind: 'prediction',
        subject_id: 'run-1',
        audit_url: '/meeting/run-1',
        audit_label: '查看投委会审计详情',
        spans: [{
          span_id: 'span1', parent_span_id: null, node_id: 'macro_policy_agent',
          name: '宏观政策 Agent', span_kind: 'agent', status: 'completed',
          started_at: trace.started_at, completed_at: trace.completed_at, duration_ms: 2000,
          agent_id: 'macro_policy_agent', agent_version: '1.0.0', model_name: 'gpt-test', prompt_version: 'v1',
          input_tokens: null, output_tokens: null, total_tokens: null, estimated_cost_usd: null,
          input_digest: '1'.repeat(64), output_digest: '2'.repeat(64),
          tool_name: 'provider.research', input_summary: '读取冻结输入。',
          output_summary: '形成一条角色意见。', summary: '形成一条角色意见。',
          error_code: null, error_summary: null, attributes: {}, references: [{
            wiki_entry_id: 'VC-WIKI-MACRO-POLICY', wiki_title: '宏观政策框架',
            wiki_version: '1.1.2', section: 'policy-transmission', content_hash: '3'.repeat(64),
            evidence_item_id: 'EV-20260807-001', evidence_content_hash: '4'.repeat(64),
            source_url: 'https://example.com/evidence', published_at: '2026-08-07T10:00:00+08:00',
          }],
        }],
      })
      return Promise.reject(new Error(`unexpected request ${url}`))
    }))

    const first = renderRoute('/observability')
    expect(await screen.findByRole('heading', { name: '运行健康与 Trace' })).toBeInTheDocument()
    expect(await screen.findByText('experiment-1')).toBeInTheDocument()
    expect(screen.getByLabelText('开始时间（从）')).toHaveAttribute('type', 'datetime-local')
    await user.click(screen.getByRole('button', { name: '加载更多 Trace' }))
    expect(await screen.findByText('experiment-2')).toBeInTheDocument()
    expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes('cursor=next-page'))).toBe(true)
    first.unmount()

    renderRoute('/traces/abc123trace')
    expect(await screen.findByRole('heading', { name: 'Agent 与确定性工具节点' })).toBeInTheDocument()
    expect(await screen.findByText('宏观政策 Agent')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /查看投委会审计详情/ })).toHaveAttribute('href', '/meeting/run-1')
    await user.click(screen.getByText('宏观政策 Agent'))
    expect(await screen.findByRole('link', { name: /宏观政策框架/ })).toHaveAttribute(
      'href',
      '/wiki?entry=VC-WIKI-MACRO-POLICY&section=policy-transmission',
    )
    expect(screen.getByRole('link', { name: /EV-20260807-001/ })).toHaveAttribute(
      'href',
      'https://example.com/evidence',
    )
    expect(screen.getByText('Trace 不保存完整 prompt')).toBeInTheDocument()
  })
})
