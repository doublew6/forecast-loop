export type Horizon = 'D1' | 'D2'
export type OutcomeLabel = 'up' | 'neutral' | 'down'
export type PredictionDirection = 'up' | 'down'
// Read-only history created before decision schema v0.3.0 can still contain
// neutral as a stored direction, so display/API types retain the wider union.
export type Direction = OutcomeLabel
export type RunStatus = 'completed' | 'running' | 'failed' | 'pending'
export type TaskStatus = 'queued' | 'running' | 'retry_wait' | 'completed' | 'failed'
export type AgentSourceType = 'ai' | 'manual' | 'quant' | 'deterministic'
export type AgentWorkflowRole = 'research' | 'strategy' | 'critic' | 'decision' | 'shadow'
export type ReflectionSeverity = 'noise' | 'directional' | 'large' | 'extreme' | 'unknown'
export type ReflectionScope = 'agent' | 'committee' | 'market_event'
export type ReflectionStatus =
  | 'awaiting_sources'
  | 'awaiting_analysis'
  | 'completed'
  | 'failed'
  | 'blocked_upstream'
export type AvailabilityClass =
  | 'available_used'
  | 'available_missed'
  | 'coverage_gap_pre_cutoff'
  | 'post_cutoff_event'
  | 'after_close_explanation'
  | 'unresolved'
export type CausalLevel = 'verified' | 'supported' | 'hypothesis' | 'unresolved'
export type LessonStatus =
  | 'candidate'
  | 'active'
  | 'proposed'
  | 'promoted'
  | 'challenged'
  | 'retired'
  | 'superseded'
export type LessonLifecycleEventType =
  | 'replay_recorded'
  | 'approved'
  | 'revalidated'
  | 'challenged'
  | 'retired'
  | 'superseded'

export interface Probabilities {
  up: number
  neutral: number
  down: number
}

export interface Citation {
  id: string
  title: string
  kind: 'wiki' | 'source'
  excerpt?: string
  wiki_entry_id?: string
  section?: string
  version?: string
  source_url?: string
  publisher?: string
  published_at?: string
  content_hash?: string
}

export interface ForecastEvaluation {
  actual_return: number
  label: Direction
  correct: boolean
  brier: number
}

export interface Forecast {
  id: string
  index_code: string
  index_name: string
  horizon: Horizon
  target_date?: string
  direction: Direction
  probabilities: Probabilities
  threshold: number
  confidence: number
  rationale: string
  citations: Citation[]
  evaluation?: ForecastEvaluation
}

export interface ForecastBatch {
  run_id: string
  as_of: string
  data_cutoff: string
  forecasts: Forecast[]
}

export interface MarketUniverseInstrument {
  code: string
  name: string
  asset_type: 'index' | 'equity'
  exchange: string
  currency: string
  sector: string | null
  strategy_bucket: 'large_cap' | 'mid_small_cap' | 'growth' | 'balanced'
  tags: string[]
  wiki_entry_ids: Record<string, string>
  agent_briefs: Record<string, string>
}

export interface MarketUniverse {
  schema_version: 'forecast-loop.market-universe/v1'
  universe_id: string
  version: string
  market: string
  timezone: string
  calendar_id: string
  session_close: string
  horizons: Horizon[]
  instruments: MarketUniverseInstrument[]
  content_hash: string
}

export interface UserJudgmentTarget {
  forecast_id: string
  run_id: string
  mode: 'demo' | 'live'
  index_code: string
  index_name: string
  horizon: Horizon
  base_trade_date: string
  target_date: string
  as_of: string
  data_cutoff: string
  submission_deadline: string | null
  submission_open: boolean
  submission_note: string
  score_eligible_if_blind: boolean
  existing_judgment_id: string | null
}

export interface UserJudgmentEvaluation {
  actual_return: number
  actual_label: Direction
  sign_correct: boolean | null
  material_direction_correct: boolean | null
  observation_hash: string
  policy_version: string
  evaluated_at: string
  content_hash: string
}

export interface UserJudgment {
  id: string
  actor_id: string
  agent_id: 'user_judgment_agent'
  agent_version: string
  forecast_id: string
  run_id: string
  mode: 'demo' | 'live'
  index_code: string
  index_name: string
  horizon: Horizon
  target_date: string
  direction: PredictionDirection
  confidence: number
  rationale: string
  counter_evidence: string
  invalidation_condition: string
  blind_attestation: boolean
  submitted_at: string
  submission_deadline: string | null
  formal_score_eligible: boolean
  run_input_hash: string
  forecast_input_hash: string
  policy_version: string
  content_hash: string
  wiki_path: string
  wiki_artifact_hash: string
  wiki_url: string
  committee_direction: Direction
  committee_agreement: boolean
  evaluation: UserJudgmentEvaluation | null
}

export interface UserJudgmentCreateInput {
  forecast_id: string
  direction: PredictionDirection
  confidence: number
  rationale: string
  counter_evidence: string
  invalidation_condition: string
  blind_attestation: boolean
}

export type PredictionDailyState =
  | 'pending'
  | 'stale'
  | 'holiday'
  | 'blocked'
  | 'awaiting'
  | 'overdue'
  | 'completed'

export interface PredictionPrepareAttempt {
  attempt_id: string
  base_session: string
  attempted_at: string
  status: string
  state: PredictionDailyState
  run_id?: string | null
  run_status?: string | null
  snapshot_hash?: string | null
  error_code?: string | null
  message?: string | null
  receipt_hash: string
}

export interface PredictionStatus {
  today: {
    base_session: string
    state: PredictionDailyState
    attempted_at?: string | null
    attempt_status?: string | null
    run_id?: string | null
    run_status?: string | null
    message: string
  }
  latest_completed_run_id?: string | null
  latest_completed_as_of?: string | null
  latest_completed_data_cutoff?: string | null
  history: PredictionPrepareAttempt[]
}

export interface AgentOpinion {
  id: string
  agent_id: string
  agent_name: string
  role: string
  status: 'active' | 'placeholder' | 'abstain'
  index_code: string
  horizon: Horizon
  target_date?: string
  direction: Direction
  probabilities: Probabilities
  summary: string
  evidence: string[]
  counter_evidence: string[]
  invalidation_conditions: string[]
  citations: Citation[]
  contribution: string
  weight: number
  strategy_context?: {
    market_regime: 'risk_on' | 'balanced' | 'risk_off'
    style_bias: 'large_cap' | 'mid_small_cap' | 'growth' | 'balanced'
    relative_rank: number
    rank_tied: boolean
    allocation_score: number
  } | null
}

export interface WorkflowStep {
  id: string
  label: string
  status: RunStatus
  started_at?: string
  completed_at?: string
  detail?: string
}

export interface WorkflowTask {
  id: string
  status: TaskStatus
  stage: string
  attempt_count: number
  max_attempts: number
  available_at: string
  attempt_started_at?: string | null
  lease_expires_at?: string | null
  last_error?: string | null
  updated_at: string
}

export interface RunSummary {
  id: string
  as_of: string
  data_cutoff: string
  status: RunStatus
  duration_seconds?: number
  data_quality: 'passed' | 'warning' | 'failed'
  data_quality_details?: {
    citations_validated?: number
    wiki_entries?: number
    wiki_has_sources?: number
    future_information_check?: string
    warning?: string
  }
  mode?: 'live' | 'demo' | string
  forecasts_count?: number
  error?: string
  task?: WorkflowTask
}

export interface Meeting {
  run: RunSummary
  opinions: AgentOpinion[]
  forecasts: Forecast[]
  workflow_steps: WorkflowStep[]
}

export interface ReflectionFinding {
  id: string
  scope_type: ReflectionScope
  agent_id?: string
  agent_name?: string
  index_code?: string
  index_name?: string
  horizon?: Horizon
  forecast_id?: string
  predicted_direction?: Direction
  actual_label?: Direction
  actual_return?: number
  threshold?: number
  direction_correct?: boolean
  outcome_verdict: string
  process_verdict: string
  error_types: string[]
  availability_class: AvailabilityClass
  causal_level: CausalLevel
  causal_confidence?: number
  what_was_right: string[]
  what_was_wrong: string[]
  attribution?: string
  original_evidence_ids: string[]
  missed_evidence_ids: string[]
  used_source_ids: string[]
  invalidation_conditions_triggered: string[]
  counterfactual_direction?: Direction
  counterfactual_probabilities?: {
    up: number
    neutral: number
    down: number
  }
  counterfactual_basis?: string
  counterfactual_explanation?: string
  would_flip?: boolean
  remediation: string[]
  lesson_candidate_ids: string[]
}

export interface ReflectionOutcome {
  forecast_id: string
  index_code: string
  index_name: string
  horizon: Horizon
  predicted_direction: Direction
  actual_label: Direction
  actual_return: number
  threshold?: number
  correct: boolean | null
  brier?: number
  signed_sigma?: number
  severity: ReflectionSeverity
  systemic_extreme_down: boolean
  history_percentile?: number
  history_sample_size?: number
  data_incomplete: boolean
  amount?: number
  advancers?: number
  decliners?: number
  unchanged?: number
  limit_down_count?: number
  breadth_down_ratio?: number
  sector_contributions: Record<string, unknown>[]
  weight_contributions: Record<string, unknown>[]
  policy_version?: string
}

export interface ReflectionMetric {
  horizon: Horizon
  outcome_count: number
  sign_sample_size: number
  sign_correct: number
  sign_accuracy: number | null
  material_sample_size: number
  material_correct: number
  material_direction_accuracy: number | null
  average_brier: number | null
}

export interface ReflectionSource {
  id: string
  title: string
  summary?: string
  source_url: string
  event_time?: string
  published_at?: string
  ingested_at?: string
  source_kind?: string
  related_index_codes: string[]
  time_class:
    | 'published_before_cutoff_not_frozen'
    | 'post_cutoff_preclose'
    | 'post_close_explanation'
    | 'unresolved'
  content_hash: string
}

export interface ReflectionSummary {
  id: string
  source_run_id: string
  target_date: string
  horizon?: Horizon
  status: ReflectionStatus
  severity: ReflectionSeverity
  systemic: boolean
  supersedes_id?: string
  prepared_at?: string
  completed_at?: string
  summary: string
  finding_count: number
  lesson_candidate_count: number
}

export interface ReflectionDetail extends ReflectionSummary {
  prediction_cutoff?: string
  reflection_cutoff?: string
  input_hash?: string
  source_snapshot_hash?: string
  evaluation_set_hash?: string
  receipt_hash?: string
  schema_version?: string
  analysis_hash?: string
  output_hash?: string
  error?: string
  data_quality?: Record<string, unknown>
  findings: ReflectionFinding[]
  outcomes: ReflectionOutcome[]
  metrics: ReflectionMetric[]
  decision_chain: WorkflowStep[]
  source_timeline: ReflectionSource[]
  lesson_candidate_ids: string[]
}

export interface LessonProposal {
  id: string
  title: string
  status: LessonStatus
  summary: string
  error_type?: string
  target_wiki_entry_id?: string
  proposed_version?: string
  independent_episode_count: number
  support_count: number
  counterexample_count: number
  source_reflection_ids: string[]
  review_after?: string
  promoted_at?: string
  last_supported_at?: string
  proposal_type?: string
  half_life_sessions: number
  replay_target_dates: number
  replay_metrics?: Record<string, unknown>
  reviewed_at?: string
  supersedes_id?: string
  superseded_by_id?: string
  replay_batch_count: number
  latest_replay_hash?: string
  wiki_review_ready: boolean | null
  replay_blockers: string[]
  revalidation_due: boolean
  revalidation_due_reasons: string[]
  lifecycle_history: LessonLifecycleEvent[]
}

export interface LessonLifecycleEvent {
  id: string
  event_type: LessonLifecycleEventType
  from_status: LessonStatus
  to_status: LessonStatus
  actor: string
  reason: string
  payload_hash: string
  occurred_at: string
}

export interface CalibrationPoint {
  bucket: string
  predicted: number
  observed: number
  count: number
}

export interface AgentScorecard {
  agent_id: string
  agent_name: string
  role: string
  workflow_role: AgentWorkflowRole
  source_type: AgentSourceType
  status: 'active' | 'placeholder' | 'shadow'
  horizon: Horizon
  accuracy: number | null
  sign_sample_size: number
  sign_correct: number
  sign_accuracy: number | null
  material_sample_size: number
  material_correct: number
  material_direction_accuracy: number | null
  brier: number | null
  sample_size: number
  sample_sufficient: boolean
  expected_calibration_error: number | null
  agent_version: string
  model_name: string | null
  note: string
  up_precision: number | null
  neutral_precision: number | null
  down_precision: number | null
  calibration: CalibrationPoint[]
}

export interface WikiSource {
  id: string
  title: string
  publisher: string
  url: string
  published_at: string
  content_hash: string
}

export interface WikiEntry {
  id: string
  slug: string
  title: string
  category: string
  version: string
  updated_at: string
  summary: string
  sections: Array<{
    id: string
    heading: string
    content: string
  }>
  sources: WikiSource[]
  cited_by_count: number
}

export interface ApiEnvelope<T> {
  data: T
  mode: 'live' | 'demo'
  demo_reason?: 'server' | 'fallback'
  error?: string
}

// Daily Reflection v1 is intentionally governed as a fixed five-index A-share
// protocol. Prediction and judgment screens use the run's dynamic instruments.
export const INDEXES = [
  { code: '000300.SH', name: '沪深300' },
  { code: '000905.SH', name: '中证500' },
  { code: '000852.SH', name: '中证1000' },
  { code: '399006.SZ', name: '创业板指' },
  { code: '000688.SH', name: '科创50' },
] as const
