import type {
  CausalStatus, FeedbackEvent, FeedbackHealth, IssueType, MaintenanceJob,
  Publication, WikiEntry, WikiSource, WorkbenchData,
} from './types'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? ''

interface RawWikiEntry {
  id: string; title: string; version: string; content_hash: string; updated_at?: string | null
  tags: string[]; body?: string | null; referenced_by_count: number
  source_ids?: string[]; source_urls?: string[]
  sections: Array<{ slug: string; title: string; excerpt: string; content?: string | null }>
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? 'GET').toUpperCase()
  const headers = new Headers(init?.headers)
  headers.set('Accept', 'application/json')
  headers.set('Content-Type', 'application/json')
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    headers.set('X-Forecast-Request', 'same-origin')
  }
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const payload = await response.json() as { detail?: unknown }
      if (typeof payload.detail === 'string') detail = payload.detail
      if (Array.isArray(payload.detail)) {
        detail = payload.detail.map((item) => (
          item && typeof item === 'object' && 'msg' in item ? String(item.msg) : ''
        )).filter(Boolean).join('；') || detail
      }
    } catch { /* retain HTTP status */ }
    throw new Error(`API ${response.status}: ${detail}`)
  }
  return response.json() as Promise<T>
}

function adaptEntry(raw: RawWikiEntry): WikiEntry {
  return {
    id: raw.id,
    title: raw.title,
    version: raw.version,
    contentHash: raw.content_hash,
    updatedAt: raw.updated_at ?? '未记录',
    summary: raw.sections[0]?.excerpt ?? raw.body?.slice(0, 240) ?? '暂无摘要',
    tags: raw.tags,
    citedByCount: raw.referenced_by_count,
    sourceIds: raw.source_ids ?? [],
    sourceUrls: raw.source_urls ?? [],
    sections: raw.sections.map((section) => ({
      id: section.slug,
      heading: section.title,
      content: section.content ?? section.excerpt,
    })),
  }
}

export async function loadWorkbench(): Promise<WorkbenchData> {
  const [healthResponse, entryResponse, sourceResponse, eventResponse, caseResponse, jobResponse, publicationResponse] = await Promise.all([
    request<{ mode: string }>('/api/health'),
    request<{ items: RawWikiEntry[] }>('/api/wiki'),
    request<{ items: WikiSource[] }>('/api/wiki/sources'),
    request<{ items: FeedbackEvent[] }>('/api/wiki/feedback/events'),
    request<{ items: FeedbackHealth[] }>('/api/wiki/feedback/health'),
    request<{ items: MaintenanceJob[] }>('/api/wiki/maintenance/jobs'),
    request<{ items: Publication[] }>('/api/wiki/publications'),
  ])
  return {
    mode: healthResponse.mode,
    entries: entryResponse.items.map(adaptEntry),
    sources: sourceResponse.items,
    events: eventResponse.items,
    health: caseResponse.items,
    jobs: jobResponse.items,
    publications: publicationResponse.items,
  }
}

export function createChallenge(payload: {
  entryId: string; entryVersion: string; section: string; issueType: Exclude<IssueType, 'right_reason'>
  causalStatus: Exclude<CausalStatus, 'unresolved'>; confidence: number; sourceIds: string[]; summary: string
}): Promise<FeedbackEvent> {
  return request('/api/wiki/feedback/events', {
    method: 'POST',
    body: JSON.stringify({
      idempotency_key: `wiki-ui-${Date.now()}-${crypto.randomUUID().slice(0, 8)}`,
      entry_id: payload.entryId,
      entry_version: payload.entryVersion,
      section: payload.section,
      signal: 'challenge',
      issue_type: payload.issueType,
      causal_status: payload.causalStatus,
      attribution_confidence: payload.confidence,
      source_ids: payload.sourceIds,
      summary: payload.summary,
    }),
  })
}

export function syncReflectionFeedback(): Promise<{ reflections_scanned: number; events_created: number; events_existing: number }> {
  return request('/api/wiki/feedback/sync', { method: 'POST', body: '{}' })
}

export function materializeFeedback(): Promise<{ items: Array<{ case_id: string; source_id: string; status: string }> }> {
  return request('/api/wiki/feedback/materialize', { method: 'POST', body: '{}' })
}

export function uploadSource(payload: {
  title: string; filename: string; media_type: string; content_base64: string
}): Promise<WikiSource> {
  return request('/api/wiki/sources', { method: 'POST', body: JSON.stringify(payload) })
}

export function prepareWikiJob(sourceIds: string[]): Promise<MaintenanceJob> {
  return request('/api/wiki/maintenance/jobs', {
    method: 'POST', body: JSON.stringify({ source_ids: sourceIds }),
  })
}
