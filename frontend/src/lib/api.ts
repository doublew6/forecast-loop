import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { demoForecastBatch, demoMeeting, demoRuns, demoScorecards, demoWiki } from './demo'
import type {
  AgentOpinion,
  AgentScorecard,
  AgentSourceType,
  AgentWorkflowRole,
  ApiEnvelope,
  Citation,
  Direction,
  Forecast,
  Horizon,
  LessonLifecycleEvent,
  LessonProposal,
  Meeting,
  MarketUniverse,
  PredictionStatus,
  ReflectionDetail,
  ReflectionFinding,
  ReflectionMetric,
  ReflectionOutcome,
  ReflectionSource,
  ReflectionSeverity,
  ReflectionSummary,
  RunStatus,
  RunSummary,
  TaskStatus,
  UserJudgment,
  UserJudgmentCreateInput,
  UserJudgmentTarget,
  WikiEntry,
  WorkflowStep,
} from './types'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? ''

interface RawCitation {
  wiki_entry_id: string
  wiki_title: string
  wiki_version: string
  section: string
  quote?: string
  wiki_quote?: string | null
  content_hash: string
  source_urls?: string[]
  evidence_item_id?: string | null
  source_url?: string | null
  evidence_content_hash?: string | null
  event_time?: string | null
  published_at?: string | null
}

interface RawForecast extends Omit<Forecast, 'citations'> {
  citations: RawCitation[]
}

interface RawAgentOpinion extends Omit<AgentOpinion, 'citations' | 'status'> {
  citations: RawCitation[]
  status: string
}

interface RawDataQuality {
  status?: string
  market_data?: string
  wiki_entries?: number
  wiki_has_sources?: number
  future_information_check?: string
  citations_validated?: number
  warning?: string | null
}

interface RawRun {
  id: string
  as_of: string
  data_cutoff: string
  status: string
  mode?: string
  duration_seconds?: number | null
  error?: string | null
  data_quality?: RawDataQuality
  workflow_steps?: RawWorkflowStep[]
  forecasts_count?: number
  task?: RawWorkflowTask | null
}

interface RawWorkflowTask {
  id: string
  status: string
  stage: string
  attempt_count: number
  max_attempts: number
  available_at: string
  attempt_started_at?: string | null
  lease_expires_at?: string | null
  last_error?: string | null
  updated_at: string
}

interface RawWorkflowStep {
  id: string
  label: string
  status: string
  started_at?: string | null
  completed_at?: string | null
}

interface RawMeeting {
  run: RawRun
  opinions: RawAgentOpinion[]
  forecasts: RawForecast[]
  workflow_steps: RawWorkflowStep[]
}

interface RawAgent {
  id: string
  name: string
  role: string
  kind: string
  workflow_role: AgentWorkflowRole
  source_type: AgentSourceType
  version: string
  weight: number
  status: string
}

interface RawDirectionMetric {
  label: Direction
  predicted: number
  actual: number
  true_positive: number
  precision: number | null
  recall: number | null
}

interface RawScorecard {
  agent_id: string
  index_code?: string | null
  horizon?: Horizon | null
  sample_size: number
  sample_sufficient: boolean
  accuracy?: number | null
  sign_sample_size?: number
  sign_correct?: number
  sign_accuracy?: number | null
  material_sample_size?: number
  material_correct?: number
  material_direction_accuracy?: number | null
  average_brier: number | null
  direction_metrics: RawDirectionMetric[]
  calibration?: Array<{
    bucket: string
    predicted: number
    observed: number
    count: number
  }>
  expected_calibration_error: number | null
  agent_version: string
  model_name: string | null
  note: string
}

interface RawWikiEntry {
  id: string
  title: string
  version: string
  updated_at?: string | null
  status: string
  owners: string[]
  tags: string[]
  source_urls: string[]
  sections: Array<{ slug: string; title: string; excerpt: string }>
  content_hash: string
  referenced_by_count: number
  body?: string | null
}

interface RawReflectionFinding {
  id?: string
  scope_type?: string
  scope?: string
  agent_id?: string | null
  agent_name?: string | null
  index_code?: string | null
  index_name?: string | null
  horizon?: string | null
  forecast_id?: string | null
  predicted_direction?: string | null
  actual_label?: string | null
  actual_return?: number | null
  threshold?: number | null
  direction_correct?: boolean | null
  correct?: boolean | null
  outcome_verdict?: string | null
  process_verdict?: string | null
  error_types?: string[] | null
  primary_error_type?: string | null
  secondary_error_types?: string[] | null
  availability_class?: string | null
  causal_level?: string | null
  causal_status?: string | null
  confidence?: number | null
  subject_id?: string | null
  verdict?: string | null
  what_was_right?: string[] | string | null
  what_was_wrong?: string[] | string | null
  attribution?: string | null
  summary?: string | null
  original_evidence_item_ids?: string[] | null
  missed_evidence_item_ids?: string[] | null
  source_ids?: string[] | null
  missed_evidence_ids?: string[] | null
  used_source_ids?: string[] | null
  evidence_ids?: string[] | null
  invalidation_conditions_triggered?: string[] | string | null
  counterfactual_direction?: string | null
  counterfactual?: {
    direction?: string | null
    probabilities?: {
      up?: number
      neutral?: number
      down?: number
    } | null
    would_flip?: boolean | null
    causal_label?: string | null
    basis?: string | null
    explanation?: string | null
  } | null
  would_flip?: boolean | null
  remediation?: string[] | string | null
  lesson_candidate_ids?: string[] | null
}

interface RawReflectionOutcome {
  forecast_id?: string
  index_code?: string
  index_name?: string
  horizon?: string
  predicted_direction?: string
  direction?: string
  actual_label?: string
  actual_return?: number
  threshold?: number | null
  correct?: boolean
  direction_correct?: boolean
  brier?: number | null
  brier_score?: number | null
  signed_sigma?: number | null
  severity?: string | null
  systemic_extreme_down?: boolean
  history_percentile?: number | null
  policy_version?: string | null
  diagnostic?: {
    signed_sigma?: number | null
    severity?: string | null
    systemic_extreme_down?: boolean
    history_percentile?: number | null
    history_percentile_rank?: number | null
    historical_abs_return_percentile?: number | null
    history_sample_size?: number | null
    data_incomplete?: boolean
    brier_score?: number | null
    sign_correct?: boolean | null
    material_direction_correct?: boolean | null
    policy_version?: string | null
  } | null
  market_snapshot?: {
    actual_return?: number | null
    base_close?: number | null
    target_close?: number | null
    amount?: number | null
    advancers?: number | null
    decliners?: number | null
    unchanged?: number | null
    limit_down_count?: number | null
    breadth_down_ratio?: number | null
    sector_contributions?: Array<Record<string, unknown>> | null
    weight_contributions?: Array<Record<string, unknown>> | null
    history_sample_size?: number | null
  } | null
}

interface RawReflectionMetric {
  horizon?: string
  outcome_count?: number
  sign_sample_size?: number
  sign_correct?: number
  sign_accuracy?: number | null
  material_sample_size?: number
  material_correct?: number
  material_direction_accuracy?: number | null
  evaluated?: number
  sample_size?: number
  correct?: number
  accuracy?: number | null
  average_brier?: number | null
  brier?: number | null
  directional_misses?: number
  noise_outcomes?: number
}

interface RawReflectionSource {
  id?: string
  title?: string
  summary?: string | null
  source_url?: string
  event_time?: string | null
  published_at?: string | null
  ingested_at?: string | null
  source_kind?: string | null
  related_index_codes?: string[] | null
  time_class?: string | null
  content_hash?: string
}

interface RawReflectionAggregateMetrics {
  outcome_count?: number
  sign_sample_size?: number
  sign_correct?: number
  sign_accuracy?: number | null
  material_sample_size?: number
  material_correct?: number
  material_direction_accuracy?: number | null
  average_brier?: number | null
}

interface RawReflection {
  id: string
  source_run_id?: string
  run_id?: string
  target_date?: string
  horizon?: string | null
  status?: string
  severity?: string
  overall_severity?: string
  systemic?: boolean
  supersedes_id?: string | null
  prepared_at?: string | null
  completed_at?: string | null
  created_at?: string | null
  prediction_cutoff?: string | null
  reflection_cutoff?: string | null
  input_hash?: string | null
  source_snapshot_hash?: string | null
  evaluation_set_hash?: string | null
  analysis_hash?: string | null
  output_hash?: string | null
  receipt_hash?: string | null
  schema_version?: string | null
  error?: string | null
  data_quality?: Record<string, unknown> | null
  summary?: string | null
  finding_count?: number
  lesson_candidate_count?: number
  lesson_candidate_ids?: string[] | null
  findings?: RawReflectionFinding[] | null
  agent_findings?: RawReflectionFinding[] | null
  committee_findings?: RawReflectionFinding[] | null
  market_findings?: RawReflectionFinding[] | null
  outcomes?: RawReflectionOutcome[] | null
  metrics?: RawReflectionMetric[] | RawReflectionAggregateMetrics | null
  decision_chain?: Array<RawWorkflowStep | RawReflectionFinding> | null
  source_timeline?: RawReflectionSource[] | null
  diagnostics?: RawReflectionOutcome[] | null
  lesson_proposals?: RawLessonProposal[] | null
}

interface RawLessonProposal {
  id: string
  reflection_run_id?: string | null
  title?: string
  status?: string
  summary?: string | null
  error_type?: string | null
  target_wiki_entry_id?: string | null
  proposed_version?: string | null
  independent_episode_count?: number
  episode_count?: number
  support_count?: number
  counterexample_count?: number
  source_reflection_ids?: string[] | null
  review_after?: string | null
  promoted_at?: string | null
  last_supported_at?: string | null
  proposal_type?: string | null
  half_life_sessions?: number
  replay_target_dates?: number | string[] | null
  replay_metrics?: Record<string, unknown> | null
  reviewed_at?: string | null
  supersedes_id?: string | null
  superseded_by_id?: string | null
  replay_batch_count?: number
  latest_replay_hash?: string | null
  revalidation_due?: boolean
  revalidation_due_reasons?: string[] | null
  lifecycle_history?: RawLessonLifecycleEvent[] | null
}

interface RawLessonLifecycleEvent {
  id?: string
  event_type?: string
  from_status?: string
  to_status?: string
  actor?: string
  reason?: string
  payload_hash?: string
  occurred_at?: string
}

let serverModePromise: Promise<'live' | 'demo'> | undefined
const staticDemoBuild = import.meta.env.VITE_STATIC_DEMO === 'true'

function readableError(error: unknown): string {
  if (error instanceof Error) return error.message
  return 'API 暂时不可用'
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (staticDemoBuild) {
    throw new Error('静态 Demo 构建不会连接 API')
  }
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      ...init?.headers,
    },
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const payload = await response.json() as { detail?: unknown }
      if (typeof payload.detail === 'string' && payload.detail.trim()) detail = payload.detail
      if (Array.isArray(payload.detail)) {
        const messages = payload.detail
          .map((item) => {
            if (!item || typeof item !== 'object') return null
            const issue = item as { loc?: unknown; msg?: unknown }
            if (typeof issue.msg !== 'string') return null
            const location = Array.isArray(issue.loc)
              ? issue.loc.filter((part) => part !== 'body').join('.')
              : ''
            return location ? `${location}: ${issue.msg}` : issue.msg
          })
          .filter((item): item is string => Boolean(item))
        if (messages.length) detail = messages.join('；')
      }
    } catch {
      // Keep the HTTP status text when the server does not return JSON.
    }
    throw new Error(`API ${response.status}: ${detail}`)
  }
  return response.json() as Promise<T>
}

async function serverMode(): Promise<'live' | 'demo'> {
  if (!serverModePromise) {
    serverModePromise = request<{ mode: string }>('/api/health')
      .then((value) => {
        if (value.mode === 'blocked-live' || value.mode === 'blocked-codex-file') {
          throw new Error('正式模式缺少经过校验的 Evidence Snapshot 或文件交接配置')
        }
        return value.mode === 'live' || value.mode === 'experimental-live' || value.mode === 'codex-file'
          ? 'live'
          : 'demo'
      })
      .catch((error) => {
        serverModePromise = undefined
        throw error
      })
  }
  return serverModePromise
}

async function loadEnvelope<T>(
  loader: () => Promise<T>,
  validate?: (value: T) => boolean,
): Promise<ApiEnvelope<T>> {
  const [data, mode] = await Promise.all([loader(), serverMode()])
  if (validate && !validate(data)) throw new Error('API 响应缺少必要字段')
  return mode === 'demo'
    ? {
        data,
        mode,
        demo_reason: 'server',
        error: '后端当前使用离线演示 Provider',
      }
    : { data, mode }
}

async function withFallback<T>(
  loader: () => Promise<T>,
  fallback: T,
  validate?: (value: T) => boolean,
): Promise<ApiEnvelope<T>> {
  try {
    return await loadEnvelope(loader, validate)
  } catch (error) {
    return {
      data: fallback,
      mode: 'demo',
      demo_reason: 'fallback',
      error: readableError(error),
    }
  }
}

function unwrapItems<T>(payload: T[] | { items: T[] } | { data: T[] }): T[] {
  if (Array.isArray(payload)) return payload
  if ('items' in payload) return payload.items
  return payload.data
}

function sourceName(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return '原始来源'
  }
}

export function adaptCitations(rawCitations: RawCitation[]): Citation[] {
  return rawCitations.flatMap((raw, citationIndex) => {
    const wiki: Citation = {
      id: `${raw.wiki_entry_id}-${raw.wiki_version}-${raw.section}-${citationIndex}`,
      title: raw.wiki_title,
      kind: 'wiki',
      excerpt: raw.wiki_quote ?? raw.quote,
      wiki_entry_id: raw.wiki_entry_id,
      section: raw.section,
      version: raw.wiki_version,
      published_at: raw.published_at ?? undefined,
      content_hash: raw.content_hash,
    }
    const evidenceSource: Citation[] = raw.source_url
      ? [{
          id: `${raw.wiki_entry_id}-evidence-${raw.evidence_item_id ?? citationIndex}`,
          title: raw.evidence_item_id ?? sourceName(raw.source_url),
          kind: 'source',
          publisher: sourceName(raw.source_url),
          source_url: raw.source_url,
          excerpt: raw.quote,
          published_at: raw.published_at ?? raw.event_time ?? undefined,
          content_hash: raw.evidence_content_hash ?? undefined,
        }]
      : []
    const wikiSources: Citation[] = (raw.source_urls ?? [])
      .filter((url) => url !== raw.source_url)
      .map((url, sourceIndex) => ({
      id: `${raw.wiki_entry_id}-source-${sourceIndex}`,
      title: sourceName(url),
      kind: 'source',
      publisher: sourceName(url),
      source_url: url,
      excerpt: `Wiki ${raw.wiki_entry_id}@${raw.wiki_version} 的方法参考源`,
    }))
    return [wiki, ...evidenceSource, ...wikiSources]
  })
}

function adaptForecast(raw: RawForecast): Forecast {
  return { ...raw, citations: adaptCitations(raw.citations ?? []) }
}

function adaptOpinion(raw: RawAgentOpinion): AgentOpinion {
  const status = raw.status === 'placeholder'
    ? 'placeholder'
    : raw.status === 'abstain'
      ? 'abstain'
      : 'active'
  return { ...raw, status, citations: adaptCitations(raw.citations ?? []) }
}

function adaptStatus(status: string): RunStatus {
  if (status === 'awaiting_draft' || status === 'queued') return 'pending'
  if (status === 'running' || status === 'completed' || status === 'failed') return status
  return 'pending'
}

function adaptTaskStatus(status: string): TaskStatus {
  if (
    status === 'queued'
    || status === 'running'
    || status === 'retry_wait'
    || status === 'completed'
    || status === 'failed'
  ) return status
  return 'queued'
}

function adaptQuality(raw: RawDataQuality | undefined, runStatus: RunStatus): RunSummary['data_quality'] {
  if (runStatus === 'failed') return 'failed'
  if (raw?.warning) return 'warning'
  if (raw?.future_information_check === 'passed' && (raw.citations_validated ?? 0) > 0) return 'passed'
  return runStatus === 'completed' ? 'warning' : 'passed'
}

export function adaptRun(raw: RawRun): RunSummary {
  const status = adaptStatus(raw.status)
  return {
    id: raw.id,
    as_of: raw.as_of,
    data_cutoff: raw.data_cutoff,
    status,
    mode: raw.mode,
    duration_seconds: raw.duration_seconds ?? undefined,
    error: raw.error ?? undefined,
    forecasts_count: raw.forecasts_count,
    task: raw.task
      ? {
          ...raw.task,
          status: adaptTaskStatus(raw.task.status),
        }
      : undefined,
    data_quality: adaptQuality(raw.data_quality, status),
    data_quality_details: {
      citations_validated: raw.data_quality?.citations_validated,
      wiki_entries: raw.data_quality?.wiki_entries,
      wiki_has_sources: raw.data_quality?.wiki_has_sources,
      future_information_check: raw.data_quality?.future_information_check,
      warning: raw.data_quality?.warning ?? undefined,
    },
  }
}

function adaptWorkflowStep(raw: RawWorkflowStep): WorkflowStep {
  return {
    id: raw.id,
    label: raw.label,
    status: adaptStatus(raw.status),
    started_at: raw.started_at ?? undefined,
    completed_at: raw.completed_at ?? undefined,
  }
}

function adaptMeeting(raw: RawMeeting): Meeting {
  return {
    run: adaptRun(raw.run),
    opinions: (raw.opinions ?? []).map(adaptOpinion),
    forecasts: (raw.forecasts ?? []).map(adaptForecast),
    workflow_steps: (raw.workflow_steps ?? raw.run.workflow_steps ?? []).map(adaptWorkflowStep),
  }
}

function adaptScorecard(raw: RawScorecard, agent: RawAgent, horizon: Horizon): AgentScorecard {
  const precision = (direction: Direction) =>
    raw.direction_metrics.find((metric) => metric.label === direction)?.precision ?? null
  const signSampleSize = raw.sign_sample_size ?? raw.sample_size
  const signAccuracy = raw.sign_accuracy ?? raw.accuracy ?? null
  const signCorrect = raw.sign_correct
    ?? (signAccuracy === null ? 0 : Math.round(signAccuracy * signSampleSize))
  const materialSampleSize = raw.material_sample_size ?? raw.sample_size
  const materialAccuracy = raw.material_direction_accuracy ?? raw.accuracy ?? null
  const materialCorrect = raw.material_correct
    ?? (materialAccuracy === null ? 0 : Math.round(materialAccuracy * materialSampleSize))
  return {
    agent_id: raw.agent_id,
    agent_name: agent.name,
    role: agent.role,
    workflow_role: agent.workflow_role,
    source_type: agent.source_type,
    status: agent.status === 'placeholder' || agent.status === 'unavailable'
      ? 'placeholder'
      : agent.status === 'shadow'
        ? 'shadow'
        : 'active',
    horizon,
    accuracy: signAccuracy,
    sign_sample_size: signSampleSize,
    sign_correct: signCorrect,
    sign_accuracy: signAccuracy,
    material_sample_size: materialSampleSize,
    material_correct: materialCorrect,
    material_direction_accuracy: materialAccuracy,
    brier: raw.average_brier,
    sample_size: raw.sample_size,
    sample_sufficient: raw.sample_sufficient,
    expected_calibration_error: raw.expected_calibration_error,
    agent_version: raw.agent_version,
    model_name: raw.model_name,
    note: raw.note,
    up_precision: precision('up'),
    neutral_precision: precision('neutral'),
    down_precision: precision('down'),
    calibration: raw.calibration ?? [],
  }
}

function adaptWikiEntry(raw: RawWikiEntry): WikiEntry {
  const category = raw.tags[0] ?? raw.status
  const summary = raw.sections[0]?.excerpt ?? raw.body?.slice(0, 240) ?? '暂无摘要'
  return {
    id: raw.id,
    slug: raw.id.toLowerCase(),
    title: raw.title,
    category,
    version: raw.version,
    updated_at: raw.updated_at ?? '未记录',
    summary,
    sections: raw.sections.map((section) => ({
      id: section.slug,
      heading: section.title,
      content: section.excerpt,
    })),
    sources: raw.source_urls.map((url, index) => ({
      id: `${raw.id}-source-${index}`,
      title: sourceName(url),
      publisher: sourceName(url),
      url,
      published_at: '时间见原文',
      content_hash: raw.content_hash,
    })),
    cited_by_count: raw.referenced_by_count,
  }
}

function stringList(value: unknown): string[] {
  if (Array.isArray(value)) return value.flatMap(stringList)
  if (typeof value === 'string') return value.trim() ? [value.trim()] : []
  if (value && typeof value === 'object') return Object.values(value).flatMap(stringList)
  return []
}

function direction(value: string | null | undefined): Direction | undefined {
  return value === 'up' || value === 'neutral' || value === 'down' ? value : undefined
}

function horizon(value: string | null | undefined): Horizon | undefined {
  return value === 'D1' || value === 'D2' ? value : undefined
}

function adaptSeverity(value: string | null | undefined): ReflectionSeverity {
  const normalized = value?.toLowerCase().replaceAll('-', '_')
  if (normalized === 'noise' || normalized === 'within_noise_band' || normalized === 'neutral') return 'noise'
  if (normalized === 'normal' || normalized === 'directional' || normalized === 'ordinary') return 'directional'
  if (normalized === 'large' || normalized === 'major' || normalized === 'severe') return 'large'
  if (
    normalized === 'extreme'
    || normalized === 'epic'
    || normalized === 'systemic'
    || normalized === 'systemic_extreme_down'
  ) return 'extreme'
  return 'unknown'
}

function adaptAvailability(value: string | null | undefined): ReflectionFinding['availability_class'] {
  const normalized = value?.toLowerCase().replaceAll('-', '_')
  if (normalized === 'available_used' || normalized === 'used_before_cutoff') return 'available_used'
  if (normalized === 'available_missed' || normalized === 'missed_before_cutoff') return 'available_missed'
  if (normalized === 'coverage_gap_pre_cutoff') return 'coverage_gap_pre_cutoff'
  if (normalized === 'post_cutoff' || normalized === 'after_cutoff' || normalized === 'post_cutoff_event') {
    return 'post_cutoff_event'
  }
  if (normalized === 'after_close_explanation') return 'after_close_explanation'
  return 'unresolved'
}

function adaptCausalLevel(value: string | null | undefined): ReflectionFinding['causal_level'] {
  const normalized = value?.toLowerCase()
  if (normalized === 'verified' || normalized === 'confirmed') return 'verified'
  if (normalized === 'supported' || normalized === 'high' || normalized === 'strong') return 'supported'
  if (
    normalized === 'hypothesis'
    || normalized === 'medium'
    || normalized === 'plausible'
    || normalized === 'low'
    || normalized === 'weak'
  ) return 'hypothesis'
  return 'unresolved'
}

function adaptScope(value: string | null | undefined): ReflectionFinding['scope_type'] {
  if (value === 'agent' || value === 'committee' || value === 'market_event') return value
  if (value === 'market') return 'market_event'
  return 'market_event'
}

function adaptReflectionStatus(value: string | null | undefined): ReflectionSummary['status'] {
  if (
    value === 'awaiting_sources'
    || value === 'awaiting_analysis'
    || value === 'completed'
    || value === 'failed'
    || value === 'blocked_upstream'
  ) return value
  if (value === 'running') return 'awaiting_analysis'
  return 'awaiting_sources'
}

function adaptReflectionFinding(raw: RawReflectionFinding, index: number): ReflectionFinding {
  const actualLabel = direction(raw.actual_label)
  const predictedDirection = direction(raw.predicted_direction)
  const inferredCorrect = raw.verdict === 'right_reason' || raw.verdict === 'lucky_correct'
    ? true
    : raw.verdict === 'wrong'
      ? false
      : undefined
  const correct = raw.direction_correct ?? raw.correct ?? (
    actualLabel && predictedDirection ? actualLabel === predictedDirection : inferredCorrect
  )
  const verdictLabels: Record<string, string> = {
    right_reason: '判断正确且理由成立',
    lucky_correct: '方向命中但理由未获验证',
    wrong: '方向误判',
    wrong_noise: '噪声带内未命中',
    right_but_noise: '方向相符但结果在噪声带内',
    not_applicable: '不适用',
    unresolved: '尚未定论',
  }
  const errorTypeLabels: Record<string, string> = {
    data_coverage_failure: '数据覆盖失败',
    attention_omission: '注意力遗漏',
    reasoning_or_weighting_failure: '推理或权重错误',
    transmission_mapping: '传导映射错误',
    horizon_timing: '周期错配',
    post_cutoff_shock: '截止后冲击',
    risk_plan_failure: '风险预案失效',
    market_noise: '市场噪声',
    unresolved: '原因未决',
  }
  const rawErrorTypes = [
    ...stringList(raw.error_types ?? raw.primary_error_type),
    ...stringList(raw.secondary_error_types),
  ]
  return {
    id: raw.id ?? `finding-${index + 1}`,
    scope_type: adaptScope(raw.scope_type ?? raw.scope),
    agent_id: raw.agent_id
      ?? (adaptScope(raw.scope_type ?? raw.scope) === 'agent' ? raw.subject_id ?? undefined : undefined),
    agent_name: raw.agent_name ?? undefined,
    index_code: raw.index_code ?? undefined,
    index_name: raw.index_name ?? undefined,
    horizon: horizon(raw.horizon),
    forecast_id: raw.forecast_id ?? undefined,
    predicted_direction: predictedDirection,
    actual_label: actualLabel,
    actual_return: raw.actual_return ?? undefined,
    threshold: raw.threshold ?? undefined,
    direction_correct: correct,
    outcome_verdict: raw.outcome_verdict
      ?? (raw.verdict ? verdictLabels[raw.verdict] ?? raw.verdict : undefined)
      ?? (correct === true ? '方向命中' : correct === false ? '方向误判' : '待核验'),
    process_verdict: raw.process_verdict ?? '归因待核验',
    error_types: rawErrorTypes.map((item) => errorTypeLabels[item] ?? item),
    availability_class: adaptAvailability(raw.availability_class),
    causal_level: adaptCausalLevel(
      raw.causal_level
      ?? raw.causal_status
      ?? raw.counterfactual?.causal_label
      ?? (raw.confidence === null || raw.confidence === undefined
        ? undefined
        : raw.confidence >= 0.8 ? 'supported' : 'hypothesis'),
    ),
    causal_confidence: raw.confidence ?? undefined,
    what_was_right: stringList(raw.what_was_right),
    what_was_wrong: stringList(raw.what_was_wrong),
    attribution: raw.attribution ?? raw.summary ?? undefined,
    original_evidence_ids: stringList(raw.original_evidence_item_ids),
    missed_evidence_ids: stringList(
      raw.missed_evidence_item_ids ?? raw.missed_evidence_ids,
    ),
    used_source_ids: stringList(raw.source_ids ?? raw.used_source_ids ?? raw.evidence_ids),
    invalidation_conditions_triggered: stringList(
      raw.invalidation_conditions_triggered,
    ),
    counterfactual_direction: direction(raw.counterfactual_direction ?? raw.counterfactual?.direction),
    counterfactual_probabilities: (
      raw.counterfactual?.probabilities?.up !== undefined
      && raw.counterfactual.probabilities.neutral !== undefined
      && raw.counterfactual.probabilities.down !== undefined
    ) ? {
        up: raw.counterfactual.probabilities.up,
        neutral: raw.counterfactual.probabilities.neutral,
        down: raw.counterfactual.probabilities.down,
      } : undefined,
    counterfactual_basis: raw.counterfactual?.basis ?? undefined,
    counterfactual_explanation: raw.counterfactual?.explanation ?? undefined,
    would_flip: raw.would_flip ?? raw.counterfactual?.would_flip ?? undefined,
    remediation: stringList(raw.remediation),
    lesson_candidate_ids: stringList(raw.lesson_candidate_ids),
  }
}

function adaptReflectionOutcome(raw: RawReflectionOutcome, index: number): ReflectionOutcome | null {
  const itemHorizon = horizon(raw.horizon)
  const predicted = direction(raw.predicted_direction ?? raw.direction)
  const actual = direction(raw.actual_label)
  const actualReturn = raw.actual_return ?? raw.market_snapshot?.actual_return
  if (
    !raw.index_code
    || !itemHorizon
    || !predicted
    || !actual
    || actualReturn === undefined
    || actualReturn === null
  ) return null
  return {
    forecast_id: raw.forecast_id ?? `outcome-${index + 1}`,
    index_code: raw.index_code,
    index_name: raw.index_name ?? raw.index_code,
    horizon: itemHorizon,
    predicted_direction: predicted,
    actual_label: actual,
    actual_return: actualReturn,
    threshold: raw.threshold ?? undefined,
    correct: raw.diagnostic?.material_direction_correct !== undefined
      ? raw.diagnostic.material_direction_correct
      : raw.correct
        ?? raw.direction_correct
        ?? (actual === 'neutral' ? null : raw.diagnostic?.sign_correct ?? predicted === actual),
    brier: raw.brier ?? raw.brier_score ?? raw.diagnostic?.brier_score ?? undefined,
    signed_sigma: raw.signed_sigma ?? raw.diagnostic?.signed_sigma ?? undefined,
    severity: adaptSeverity(raw.severity ?? raw.diagnostic?.severity),
    systemic_extreme_down: raw.systemic_extreme_down
      ?? raw.diagnostic?.systemic_extreme_down
      ?? false,
    history_percentile: raw.history_percentile
      ?? raw.diagnostic?.history_percentile
      ?? raw.diagnostic?.history_percentile_rank
      ?? raw.diagnostic?.historical_abs_return_percentile
      ?? undefined,
    history_sample_size: raw.diagnostic?.history_sample_size
      ?? raw.market_snapshot?.history_sample_size
      ?? undefined,
    data_incomplete: raw.diagnostic?.data_incomplete ?? false,
    amount: raw.market_snapshot?.amount ?? undefined,
    advancers: raw.market_snapshot?.advancers ?? undefined,
    decliners: raw.market_snapshot?.decliners ?? undefined,
    unchanged: raw.market_snapshot?.unchanged ?? undefined,
    limit_down_count: raw.market_snapshot?.limit_down_count ?? undefined,
    breadth_down_ratio: raw.market_snapshot?.breadth_down_ratio ?? undefined,
    sector_contributions: raw.market_snapshot?.sector_contributions ?? [],
    weight_contributions: raw.market_snapshot?.weight_contributions ?? [],
    policy_version: raw.policy_version ?? raw.diagnostic?.policy_version ?? undefined,
  }
}

function metricFromOutcomes(itemHorizon: Horizon, outcomes: ReflectionOutcome[]): ReflectionMetric {
  const rows = outcomes.filter((item) => item.horizon === itemHorizon)
  const signRows = rows.filter((item) => item.actual_label !== 'neutral')
  const signCorrect = signRows.filter(
    (item) => item.predicted_direction === item.actual_label,
  ).length
  const materialRows = rows.filter((item) => item.correct !== null)
  const materialCorrect = materialRows.filter((item) => item.correct).length
  const brierValues = rows.flatMap((item) => item.brier === undefined ? [] : [item.brier])
  return {
    horizon: itemHorizon,
    outcome_count: rows.length,
    sign_sample_size: signRows.length,
    sign_correct: signCorrect,
    sign_accuracy: signRows.length ? signCorrect / signRows.length : null,
    material_sample_size: materialRows.length,
    material_correct: materialCorrect,
    material_direction_accuracy: materialRows.length
      ? materialCorrect / materialRows.length
      : null,
    average_brier: brierValues.length
      ? brierValues.reduce((total, value) => total + value, 0) / brierValues.length
      : null,
  }
}

function adaptReflectionMetric(raw: RawReflectionMetric): ReflectionMetric | null {
  const itemHorizon = horizon(raw.horizon)
  if (!itemHorizon) return null
  const legacySampleSize = raw.evaluated ?? raw.sample_size ?? 0
  const legacyCorrect = raw.correct ?? (raw.accuracy === null || raw.accuracy === undefined
    ? 0
    : Math.round(raw.accuracy * legacySampleSize))
  const materialSampleSize = raw.material_sample_size ?? legacySampleSize
  const materialCorrect = raw.material_correct ?? legacyCorrect
  const signSampleSize = raw.sign_sample_size ?? materialSampleSize
  const signCorrect = raw.sign_correct ?? materialCorrect
  return {
    horizon: itemHorizon,
    outcome_count: raw.outcome_count
      ?? materialSampleSize
      + (raw.noise_outcomes ?? 0),
    sign_sample_size: signSampleSize,
    sign_correct: signCorrect,
    sign_accuracy: raw.sign_accuracy
      ?? (signSampleSize ? signCorrect / signSampleSize : null),
    material_sample_size: materialSampleSize,
    material_correct: materialCorrect,
    material_direction_accuracy: raw.material_direction_accuracy
      ?? raw.accuracy
      ?? (materialSampleSize ? materialCorrect / materialSampleSize : null),
    average_brier: raw.average_brier ?? raw.brier ?? null,
  }
}

function adaptReflectionSource(raw: RawReflectionSource, index: number): ReflectionSource | null {
  if (!raw.source_url || !raw.content_hash) return null
  const timeClass = raw.time_class as ReflectionSource['time_class']
  const acceptedTimeClasses: ReflectionSource['time_class'][] = [
    'published_before_cutoff_not_frozen',
    'post_cutoff_preclose',
    'post_close_explanation',
    'unresolved',
  ]
  return {
    id: raw.id ?? `source-${index + 1}`,
    title: raw.title ?? raw.source_url,
    summary: raw.summary ?? undefined,
    source_url: raw.source_url,
    event_time: raw.event_time ?? undefined,
    published_at: raw.published_at ?? undefined,
    ingested_at: raw.ingested_at ?? undefined,
    source_kind: raw.source_kind ?? undefined,
    related_index_codes: raw.related_index_codes ?? [],
    time_class: acceptedTimeClasses.includes(timeClass) ? timeClass : 'unresolved',
    content_hash: raw.content_hash,
  }
}

function allRawFindings(raw: RawReflection): RawReflectionFinding[] {
  return [
    ...(raw.findings ?? []),
    ...(raw.agent_findings ?? []).map((item) => ({ ...item, scope_type: item.scope_type ?? 'agent' })),
    ...(raw.committee_findings ?? []).map((item) => ({ ...item, scope_type: item.scope_type ?? 'committee' })),
    ...(raw.market_findings ?? []).map((item) => ({ ...item, scope_type: item.scope_type ?? 'market' })),
  ]
}

function adaptReflectionDecisionStep(
  raw: RawWorkflowStep | RawReflectionFinding,
  index: number,
): WorkflowStep {
  if ('label' in raw) return adaptWorkflowStep(raw)
  const finding = adaptReflectionFinding(raw, index)
  const subject = finding.agent_name
    ?? finding.agent_id
    ?? finding.index_name
    ?? finding.index_code
    ?? (finding.scope_type === 'committee' ? '投委会综合' : '市场事件')
  return {
    id: finding.id,
    label: subject,
    status: 'completed',
    detail: finding.attribution ?? finding.outcome_verdict,
  }
}

function adaptReflectionSummary(raw: RawReflection): ReflectionSummary {
  const findings = allRawFindings(raw)
  const candidateIds = new Set([
    ...stringList(raw.lesson_candidate_ids),
    ...findings.flatMap((finding) => stringList(finding.lesson_candidate_ids)),
    ...(raw.lesson_proposals ?? []).map((lesson) => lesson.id),
  ])
  const severity = adaptSeverity(raw.overall_severity ?? raw.severity)
  return {
    id: raw.id,
    source_run_id: raw.source_run_id ?? raw.run_id ?? '',
    target_date: raw.target_date ?? '',
    horizon: horizon(raw.horizon),
    status: adaptReflectionStatus(raw.status ?? 'completed'),
    severity,
    systemic: raw.systemic ?? (
      raw.overall_severity === 'systemic_extreme_down'
      || raw.severity === 'systemic_extreme_down'
    ),
    supersedes_id: raw.supersedes_id ?? undefined,
    prepared_at: raw.prepared_at ?? raw.created_at ?? undefined,
    completed_at: raw.completed_at ?? undefined,
    summary: raw.summary ?? '等待复盘结论。',
    finding_count: raw.finding_count ?? findings.length,
    lesson_candidate_count: raw.lesson_candidate_count ?? candidateIds.size,
  }
}

function adaptReflectionDetail(raw: RawReflection): ReflectionDetail {
  const summary = adaptReflectionSummary(raw)
  const findings = allRawFindings(raw).map(adaptReflectionFinding)
  const explicitOutcomes = (raw.outcomes ?? [])
    .map(adaptReflectionOutcome)
    .filter((item): item is ReflectionOutcome => item !== null)
  const findingOutcomes = findings
    .map((finding, index) => adaptReflectionOutcome({
      forecast_id: finding.forecast_id,
      index_code: finding.index_code,
      index_name: finding.index_name,
      horizon: finding.horizon,
      predicted_direction: finding.predicted_direction,
      actual_label: finding.actual_label,
      actual_return: finding.actual_return,
      threshold: finding.threshold,
      direction_correct: finding.direction_correct,
    }, index))
    .filter((item): item is ReflectionOutcome => item !== null)
  const outcomes = explicitOutcomes.length ? explicitOutcomes : findingOutcomes
  const diagnostics = (raw.diagnostics ?? [])
    .map(adaptReflectionOutcome)
    .filter((item): item is ReflectionOutcome => item !== null)
  const outcomesWithDiagnostics = outcomes.map((outcome) => {
    const diagnostic = diagnostics.find((item) => item.forecast_id === outcome.forecast_id)
      ?? diagnostics.find(
        (item) => item.index_code === outcome.index_code && item.horizon === outcome.horizon,
    )
    return diagnostic ? { ...outcome, ...diagnostic } : outcome
  })
  const rawMetrics: RawReflectionMetric[] = Array.isArray(raw.metrics)
    ? raw.metrics
    : raw.metrics
      ? [{
          horizon: raw.horizon ?? undefined,
          outcome_count: raw.metrics.outcome_count,
          sign_sample_size: raw.metrics.sign_sample_size,
          sign_correct: raw.metrics.sign_correct,
          sign_accuracy: raw.metrics.sign_accuracy,
          material_sample_size: raw.metrics.material_sample_size,
          material_correct: raw.metrics.material_correct,
          material_direction_accuracy: raw.metrics.material_direction_accuracy,
          average_brier: raw.metrics.average_brier,
          noise_outcomes: (
            raw.metrics.outcome_count !== undefined
            && raw.metrics.material_sample_size !== undefined
          )
            ? raw.metrics.outcome_count - raw.metrics.material_sample_size
            : undefined,
        }]
      : []
  const explicitMetrics = rawMetrics
    .map(adaptReflectionMetric)
    .filter((item): item is ReflectionMetric => item !== null)
  const metrics = explicitMetrics.length
    ? explicitMetrics
    : (['D1', 'D2'] as Horizon[]).map((item) => metricFromOutcomes(item, outcomesWithDiagnostics))
  const lessonCandidateIds = [...new Set([
    ...stringList(raw.lesson_candidate_ids),
    ...findings.flatMap((finding) => finding.lesson_candidate_ids),
    ...(raw.lesson_proposals ?? []).map((lesson) => lesson.id),
  ])]
  const severityRank: Record<ReflectionSeverity, number> = {
    unknown: 0,
    noise: 1,
    directional: 2,
    large: 3,
    extreme: 4,
  }
  const outcomeSeverity = outcomesWithDiagnostics.reduce<ReflectionSeverity>(
    (current, outcome) => severityRank[outcome.severity] > severityRank[current]
      ? outcome.severity
      : current,
    'unknown',
  )
  return {
    ...summary,
    severity: summary.severity === 'unknown' ? outcomeSeverity : summary.severity,
    systemic: summary.systemic || outcomesWithDiagnostics.some((item) => item.systemic_extreme_down),
    prediction_cutoff: raw.prediction_cutoff ?? undefined,
    reflection_cutoff: raw.reflection_cutoff ?? raw.completed_at ?? undefined,
    input_hash: raw.input_hash ?? undefined,
    source_snapshot_hash: raw.source_snapshot_hash ?? undefined,
    evaluation_set_hash: raw.evaluation_set_hash ?? undefined,
    analysis_hash: raw.analysis_hash ?? undefined,
    output_hash: raw.output_hash ?? undefined,
    receipt_hash: raw.receipt_hash ?? undefined,
    schema_version: raw.schema_version ?? undefined,
    error: raw.error ?? undefined,
    data_quality: raw.data_quality ?? undefined,
    findings,
    outcomes: outcomesWithDiagnostics,
    metrics,
    decision_chain: (raw.decision_chain ?? []).map(adaptReflectionDecisionStep),
    source_timeline: (raw.source_timeline ?? [])
      .map(adaptReflectionSource)
      .filter((item): item is ReflectionSource => item !== null),
    lesson_candidate_ids: lessonCandidateIds,
  }
}

function adaptLessonProposal(raw: RawLessonProposal): LessonProposal {
  const statuses: LessonProposal['status'][] = [
    'candidate',
    'active',
    'proposed',
    'promoted',
    'challenged',
    'retired',
    'superseded',
  ]
  const normalized = raw.status?.toLowerCase() as LessonProposal['status']
  const replayMetrics = raw.replay_metrics ?? {}
  const wikiReviewReady = typeof replayMetrics.wiki_review_ready === 'boolean'
    ? replayMetrics.wiki_review_ready
    : null
  return {
    id: raw.id,
    title: raw.title ?? raw.id,
    status: statuses.includes(normalized) ? normalized : 'candidate',
    summary: raw.summary ?? '等待归纳。',
    error_type: raw.error_type ?? undefined,
    target_wiki_entry_id: raw.target_wiki_entry_id ?? undefined,
    proposed_version: raw.proposed_version ?? undefined,
    independent_episode_count: raw.independent_episode_count ?? raw.episode_count ?? 0,
    support_count: raw.support_count ?? raw.independent_episode_count ?? raw.episode_count ?? 0,
    counterexample_count: raw.counterexample_count
      ?? (typeof raw.replay_metrics?.counterexample_count === 'number'
        ? raw.replay_metrics.counterexample_count
        : 0),
    source_reflection_ids: raw.source_reflection_ids?.length
      ? stringList(raw.source_reflection_ids)
      : stringList(raw.reflection_run_id),
    review_after: raw.review_after ?? undefined,
    promoted_at: raw.promoted_at ?? undefined,
    last_supported_at: raw.last_supported_at ?? undefined,
    proposal_type: raw.proposal_type ?? undefined,
    half_life_sessions: raw.half_life_sessions ?? 60,
    replay_target_dates: typeof raw.replay_target_dates === 'number'
      ? raw.replay_target_dates
      : stringList(raw.replay_target_dates).length,
    replay_metrics: raw.replay_metrics ?? undefined,
    reviewed_at: raw.reviewed_at ?? undefined,
    supersedes_id: raw.supersedes_id ?? undefined,
    superseded_by_id: raw.superseded_by_id ?? undefined,
    replay_batch_count: raw.replay_batch_count ?? 0,
    latest_replay_hash: raw.latest_replay_hash ?? undefined,
    wiki_review_ready: wikiReviewReady,
    replay_blockers: stringList(replayMetrics.blockers),
    revalidation_due: raw.revalidation_due ?? false,
    revalidation_due_reasons: stringList(raw.revalidation_due_reasons),
    lifecycle_history: (raw.lifecycle_history ?? []).map(adaptLessonLifecycleEvent),
  }
}

function adaptLessonLifecycleEvent(
  raw: RawLessonLifecycleEvent,
  index: number,
): LessonLifecycleEvent {
  const eventTypes: LessonLifecycleEvent['event_type'][] = [
    'replay_recorded',
    'approved',
    'revalidated',
    'challenged',
    'retired',
    'superseded',
  ]
  const statuses: LessonProposal['status'][] = [
    'candidate',
    'active',
    'proposed',
    'promoted',
    'challenged',
    'retired',
    'superseded',
  ]
  const rawEventType = raw.event_type as LessonLifecycleEvent['event_type']
  const rawFromStatus = raw.from_status as LessonProposal['status']
  const rawToStatus = raw.to_status as LessonProposal['status']
  return {
    id: raw.id ?? `lesson-event-${index}`,
    event_type: eventTypes.includes(rawEventType) ? rawEventType : 'replay_recorded',
    from_status: statuses.includes(rawFromStatus) ? rawFromStatus : 'candidate',
    to_status: statuses.includes(rawToStatus) ? rawToStatus : 'candidate',
    actor: raw.actor ?? 'system',
    reason: raw.reason ?? '',
    payload_hash: raw.payload_hash ?? '',
    occurred_at: raw.occurred_at ?? '',
  }
}

export function useLatestForecasts(enabled = true) {
  return useQuery({
    queryKey: ['forecasts', 'latest'],
    queryFn: () =>
      withFallback(
        async () => {
          const raw = await request<{
            run_id: string
            as_of: string
            data_cutoff: string
            forecasts: RawForecast[]
          }>('/api/forecasts/latest')
          return { ...raw, forecasts: raw.forecasts.map(adaptForecast) }
        },
        demoForecastBatch,
        (value) => Boolean(value.run_id && Array.isArray(value.forecasts)),
      ),
    staleTime: 30_000,
    enabled,
  })
}

export function useMarketUniverse() {
  return useQuery({
    queryKey: ['market-universe'],
    queryFn: () =>
      loadEnvelope(
        () => request<MarketUniverse>('/api/market-universe'),
        (value) => Boolean(
          value.schema_version === 'forecast-loop.market-universe/v1'
          && value.content_hash
          && Array.isArray(value.instruments)
          && value.instruments.length > 0,
        ),
      ),
    staleTime: Number.POSITIVE_INFINITY,
  })
}

export function usePredictionStatus() {
  return useQuery({
    queryKey: ['prediction-status'],
    queryFn: () =>
      loadEnvelope(
        () => request<PredictionStatus>('/api/prediction-status'),
        (value) => Boolean(
          value.today?.base_session
          && value.today?.state
          && Array.isArray(value.history),
        ),
      ),
    refetchInterval: 30_000,
    staleTime: 15_000,
  })
}

function adaptUserJudgment(raw: UserJudgment): UserJudgment {
  const wikiUrl = raw.wiki_url.startsWith('http')
    ? raw.wiki_url
    : `${API_BASE}${raw.wiki_url}`
  return { ...raw, wiki_url: wikiUrl }
}

export function useUserJudgmentTargets() {
  return useQuery({
    queryKey: ['user-judgments', 'targets'],
    queryFn: () =>
      loadEnvelope(
        async () => unwrapItems(
          await request<{ items: UserJudgmentTarget[] }>('/api/user-judgments/targets'),
        ),
        (value) => Array.isArray(value),
      ),
    staleTime: 15_000,
  })
}

export function useUserJudgments() {
  return useQuery({
    queryKey: ['user-judgments'],
    queryFn: () =>
      loadEnvelope(
        async () => unwrapItems(
          await request<{ items: UserJudgment[] }>('/api/user-judgments'),
        ).map(adaptUserJudgment),
        (value) => Array.isArray(value),
      ),
  })
}

export function useCreateUserJudgment() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: ['user-judgments', 'create'],
    mutationFn: async (payload: UserJudgmentCreateInput) => adaptUserJudgment(
      await request<UserJudgment>('/api/user-judgments', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
    ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['user-judgments'] })
      queryClient.invalidateQueries({ queryKey: ['scorecards'] })
    },
  })
}

export function useMeeting(runId?: string, { allowDemoFallback = false } = {}) {
  const fallback = allowDemoFallback && demoMeeting.run.id === runId ? demoMeeting : undefined
  return useQuery({
    queryKey: ['meeting', runId ?? 'unresolved', fallback ? 'demo-fallback' : 'strict'],
    enabled: Boolean(runId),
    queryFn: async () => {
      if (!runId) throw new Error('缺少投委会运行 ID')
      const loader = async () => adaptMeeting(await request<RawMeeting>(`/api/meetings/${runId}`))
      const validate = (value: Meeting) => value.run?.id === runId && Array.isArray(value.opinions)
      return fallback
        ? withFallback(loader, fallback, validate)
        : loadEnvelope(loader, validate)
    },
  })
}

export function useScorecards() {
  return useQuery({
    queryKey: ['scorecards'],
    queryFn: () =>
      withFallback(
        async () => {
          const response = await request<{ items: RawAgent[] }>('/api/agents')
          const horizons: Horizon[] = ['D1', 'D2']
          const requests = response.items.flatMap((agent) =>
            horizons.map(async (horizon) => {
              const raw = await request<RawScorecard>(`/api/agents/${agent.id}/scorecard?horizon=${horizon}`)
              return adaptScorecard(raw, agent, horizon)
            }),
          )
          return Promise.all(requests)
        },
        demoScorecards,
        (value) => Array.isArray(value),
      ),
  })
}

export function useWikiEntries() {
  return useQuery({
    queryKey: ['wiki'],
    queryFn: () =>
      withFallback(
        async () => unwrapItems(await request<{ items: RawWikiEntry[] }>('/api/wiki')).map(adaptWikiEntry),
        demoWiki,
        (value) => Array.isArray(value) && value.length > 0,
      ),
  })
}

export function useRuns() {
  return useQuery({
    queryKey: ['runs'],
    queryFn: () =>
      withFallback(
        async () => unwrapItems(await request<{ items: RawRun[] }>('/api/runs')).map(adaptRun),
        demoRuns,
        (value) => Array.isArray(value),
      ),
    refetchInterval: (query) =>
      query.state.data?.data.some(
        (run) =>
          run.status === 'running'
          || run.status === 'pending'
          || run.task?.status === 'queued'
          || run.task?.status === 'running'
          || run.task?.status === 'retry_wait',
      ) ? 4_000 : false,
  })
}

export function useReflections() {
  return useQuery({
    queryKey: ['reflections'],
    queryFn: () =>
      loadEnvelope(
        async () => unwrapItems(
          await request<RawReflection[] | { items: RawReflection[] }>('/api/reflections'),
        ).map(adaptReflectionSummary),
        (value) => Array.isArray(value),
      ),
  })
}

export function useReflection(reflectionId?: string) {
  return useQuery({
    queryKey: ['reflections', reflectionId ?? 'unresolved'],
    enabled: Boolean(reflectionId),
    queryFn: async () => {
      if (!reflectionId) throw new Error('缺少复盘 ID')
      return loadEnvelope(
        async () => adaptReflectionDetail(
          await request<RawReflection>(`/api/reflections/${encodeURIComponent(reflectionId)}`),
        ),
        (value) => value.id === reflectionId,
      )
    },
  })
}

export function useLessons() {
  return useQuery({
    queryKey: ['lessons'],
    queryFn: () =>
      loadEnvelope(
        async () => unwrapItems(
          await request<RawLessonProposal[] | { items: RawLessonProposal[] }>('/api/lessons'),
        ).map(adaptLessonProposal),
        (value) => Array.isArray(value),
      ),
  })
}

export function useCreateRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async () => adaptRun(await request<RawRun>('/api/runs', { method: 'POST', body: '{}' })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['runs'] }),
  })
}

export type { RawCitation, RawScorecard, RawWikiEntry }
