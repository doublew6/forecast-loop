import { adaptCitations, adaptRun } from './api'

describe('API adapters', () => {
  it('expands a backend Wiki citation into a Wiki item and original source items', () => {
    const result = adaptCitations([
      {
        wiki_entry_id: 'VC-WIKI-AI-STORAGE',
        wiki_title: 'AI 存储产业链',
        wiki_version: '1.2.0',
        section: 'hbm-demand',
        quote: 'HBM 需求需要多源确认。',
        content_hash: 'sha256:abc',
        source_urls: ['https://investors.micron.com/results'],
      },
    ])

    expect(result).toHaveLength(2)
    expect(result[0]).toMatchObject({
      kind: 'wiki',
      wiki_entry_id: 'VC-WIKI-AI-STORAGE',
      version: '1.2.0',
      section: 'hbm-demand',
    })
    expect(result[1]).toMatchObject({
      kind: 'source',
      publisher: 'investors.micron.com',
      source_url: 'https://investors.micron.com/results',
    })
  })

  it('keeps frozen evidence provenance distinct from Wiki method sources', () => {
    const result = adaptCitations([
      {
        wiki_entry_id: 'VC-WIKI-AI-STORAGE',
        wiki_title: 'AI 存储产业链',
        wiki_version: '1.2.0',
        section: 'hbm-demand',
        quote: '公司披露 HBM 出货进度。',
        wiki_quote: '用一手披露核验需求、产能与交付。',
        content_hash: 'a'.repeat(64),
        evidence_item_id: 'MICRON-2026Q2-RESULTS',
        evidence_content_hash: 'b'.repeat(64),
        source_url: 'https://investors.micron.com/results',
        source_urls: ['https://investors.micron.com/'],
        published_at: '2026-07-13T12:00:00+08:00',
      },
    ])

    expect(result).toHaveLength(3)
    expect(result[0]).toMatchObject({
      kind: 'wiki',
      excerpt: '用一手披露核验需求、产能与交付。',
    })
    expect(result[1]).toMatchObject({
      kind: 'source',
      title: 'MICRON-2026Q2-RESULTS',
      excerpt: '公司披露 HBM 出货进度。',
      content_hash: 'b'.repeat(64),
      source_url: 'https://investors.micron.com/results',
    })
  })

  it('preserves exact persistent task states instead of collapsing retries', () => {
    const result = adaptRun({
      id: 'run-1',
      as_of: '2026-07-27T15:00:00+08:00',
      data_cutoff: '2026-07-27T15:00:00+08:00',
      status: 'queued',
      forecasts_count: 0,
      task: {
        id: 'task-1',
        status: 'retry_wait',
        stage: 'retry_wait',
        attempt_count: 1,
        max_attempts: 3,
        available_at: '2026-07-27T15:06:00+08:00',
        attempt_started_at: '2026-07-27T15:05:00+08:00',
        lease_expires_at: null,
        last_error: 'provider timeout',
        updated_at: '2026-07-27T15:05:30+08:00',
      },
    })

    expect(result.status).toBe('pending')
    expect(result.task).toMatchObject({
      status: 'retry_wait',
      stage: 'retry_wait',
      attempt_count: 1,
      max_attempts: 3,
      last_error: 'provider timeout',
    })
  })
})
