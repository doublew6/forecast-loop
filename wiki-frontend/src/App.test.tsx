import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from './App'

const responses: Record<string, unknown> = {
  '/api/health': { mode: 'live' },
  '/api/wiki': { items: [{
    id: 'VC-WIKI-MACRO-001', title: 'D1与D2流动性传导框架', version: '1.2.0',
    content_hash: 'a'.repeat(64), updated_at: '2026-08-06T06:00:00+08:00',
    tags: ['macro'], referenced_by_count: 4, source_ids: [],
    source_urls: ['https://www.pbc.gov.cn/'],
    sections: [{ slug: 'transmission', title: '传导路径', excerpt: '流动性变化通过风险偏好与估值折现影响 D1 指数方向。' }],
  }] },
  '/api/wiki/sources': { items: [] },
  '/api/wiki/feedback/events': { items: [] },
  '/api/wiki/feedback/health': { items: [{
    case_id: 'case-1', entry_id: 'VC-WIKI-MACRO-001', entry_version: '1.2.0', section: 'transmission',
    status: 'watching', current_version: true, posterior_reliability: .58,
    support_weight: .8, challenge_weight: .7, support_event_count: 1,
    challenge_event_count: 1, independent_support_episodes: 1,
    independent_challenge_episodes: 1, review_eligible: false, issue_counts: {},
  }] },
  '/api/wiki/maintenance/jobs': { items: [] },
  '/api/wiki/publications': { items: [] },
}

describe('Wiki Atlas', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('renders domains, raw materials, extracted Wiki, and a fixed D1 scope', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), 'http://localhost').pathname
      return new Response(JSON.stringify(responses[path]), {
        status: responses[path] ? 200 : 404,
        headers: { 'Content-Type': 'application/json' },
      })
    }))
    render(<App />)
    await waitFor(() => expect(screen.getAllByText('D1流动性传导框架').length).toBeGreaterThan(0))
    expect(screen.getByText('Wiki Atlas')).toBeInTheDocument()
    expect(screen.getAllByText('宏观与政策').length).toBeGreaterThan(0)
    expect(screen.getAllByText('原始材料').length).toBeGreaterThan(0)
    expect(screen.getAllByText('提取出的 Wiki').length).toBeGreaterThan(0)
    expect(screen.getByText('中国人民银行政策资料')).toBeInTheDocument()
    expect(screen.getAllByText('D1').length).toBeGreaterThan(0)
    expect(screen.getByText('下一交易日')).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/D2|主前端/)
  })
})
