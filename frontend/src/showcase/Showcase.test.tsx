import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { Showcase } from './Showcase'
import { metrics, parsePublication } from './data'
import type { PublicSnapshot } from './data'

function snapshot(): PublicSnapshot {
  return {
    schema_version: 'forecast-loop.public-showcase/v1',
    generated_at: '2026-08-13T10:00:00+08:00',
    next_update_at: '2026-08-14T10:00:00+08:00',
    mode: 'live',
    forecasts: [{
      id: 'a'.repeat(64), target_id: 'csi1000-absolute-d1', lane: 'formal',
      anchor_date: '2026-08-12', target_date: '2026-08-13',
      data_cutoff: '2026-08-12T15:00:00+08:00', published_at: '2026-08-12T20:30:00+08:00',
      probabilities: { up: 0.6, neutral: 0.25, down: 0.15 }, threshold: 0.003,
      result: { actual_return: 0.01, actual_label: 'up', brier_score: 0.245 / 3, direction_correct: true },
    }, {
      id: 'b'.repeat(64), target_id: 'csi1000-absolute-d1', lane: 'shadow',
      anchor_date: '2026-08-11', target_date: '2026-08-12',
      data_cutoff: '2026-08-11T15:00:00+08:00', published_at: '2026-08-11T20:30:00+08:00',
      probabilities: { up: 0.3, neutral: 0.2, down: 0.5 }, threshold: 0.003,
      result: { actual_return: 0.01, actual_label: 'up', brier_score: 0.78 / 3, direction_correct: false },
    }],
  }
}
describe('public showcase boundary', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })
  it('uses GET only, separates lanes and marks expired publications', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-08-15T10:00:00+08:00'))
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(snapshot())))
    vi.stubGlobal('fetch', fetcher)
    render(<Showcase />)
    expect(await screen.findByText('更新检查已超时')).toBeInTheDocument()
    expect(screen.getByText('100.0%')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Shadow' }))
    expect(screen.getByText('0.0%')).toBeInTheDocument()
    for (const [url, options] of fetcher.mock.calls) {
      expect(String(url)).toMatch(/\/forecast.json$/)
      expect(options.method).toBe('GET')
      expect(options.headers).toBeUndefined()
    }
    expect(screen.queryByText('我的判断')).not.toBeInTheDocument()
    expect(screen.queryByText('Agent 评测')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /运行|提交|注册/ })).not.toBeInTheDocument()
  })
  it('does not fabricate forecasts when the publication is absent', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('unavailable')))
    render(<Showcase />)
    expect(await screen.findByText('更新暂不可用')).toBeInTheDocument()
    expect(screen.getByText('暂无公开记录')).toBeInTheDocument()
    expect(screen.queryByText('合成数据演示')).not.toBeInTheDocument()
  })
  it('accepts an explicit waiting state and clearly labels synthetic data', async () => {
    const demo = snapshot()
    demo.mode = 'demo'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(demo))))
    render(<Showcase />)
    // Expired demo data must retain a visible synthetic-data label as well.
    await waitFor(() => expect(screen.getByText(/合成/)).toBeInTheDocument())
  })
  it('rejects unexpected fields, impossible dates and malformed probabilities', () => {
    expect(parsePublication({ schema_version: 'forecast-loop.public-showcase/v1', status: 'awaiting_publication' }))
      .toHaveProperty('status', 'awaiting_publication')
    const data = snapshot()
    expect(() => parsePublication({ ...data, api_key: 'test-only' })).toThrow()
    data.forecasts[0].probabilities.up = 0.99
    expect(() => parsePublication(data)).toThrow()
    const invalid = snapshot()
    invalid.forecasts[0].target_date = '2026-02-30'
    expect(() => parsePublication(invalid)).toThrow()
    const forged = snapshot()
    forged.forecasts[0].result!.brier_score = 0
    expect(() => parsePublication(forged)).toThrow()
  })
  it('excludes unsettled forecasts from scoring', () => {
    const rows = snapshot().forecasts
    rows[1].result = null
    expect(metrics(rows)).toEqual({ count: 1, accuracy: 1, brier: 0.245 / 3 })
  })
})

it('displays intraday supplements with actual timestamps without changing scores', async () => {
  const base = snapshot()
  const row = {
    id: 'c'.repeat(64), request_hash: 'd'.repeat(64), target_id: 'csi1000-open-to-open-d1',
    publication_kind: 'intraday_supplement', performance_included: false, evidence_status: 'partial',
    anchor_date: '2026-08-13', target_date: '2026-08-14',
    data_cutoff: '2026-08-13T10:13:16+08:00', published_at: '2026-08-13T10:14:48+08:00',
    probabilities: { up: .35, neutral: .27, down: .38 }, threshold: .005,
  }
  const data = { ...base, schema_version: 'forecast-loop.public-showcase/v2',
    generated_at: '2026-08-13T10:16:00+08:00', intraday_forecasts: [row] }
  expect(() => parsePublication(data)).not.toThrow()
  expect(() => parsePublication({ ...data, intraday_forecasts: [{ ...row, performance_included: true }] })).toThrow()
  expect(() => parsePublication({ ...data, intraday_forecasts: [{ ...row, published_at: '2026-08-13T09:15:00+08:00' }] })).toThrow()
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(data))))
  render(<Showcase />)
  expect(await screen.findByRole('heading', { name: '盘中补发预测' })).toBeInTheDocument()
  expect(screen.getByText(/10:14:48/)).toBeInTheDocument()
  expect(screen.getByText('35.0%')).toBeInTheDocument()
  expect(screen.getByText('100.0%')).toBeInTheDocument()
  expect(metrics(base.forecasts).count).toBe(2)
  vi.unstubAllGlobals()
})
