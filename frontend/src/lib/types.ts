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

export type V2Horizon = 'D1' | 'W1' | 'D20'
export type ResearchLaneV2 = 'formal' | 'shadow'

export interface ResearchInstrumentV2 {
  code: string
  name: string
  role: 'primary' | 'benchmark'
  data_required: boolean
}

export interface ResearchTargetV2 {
  target_id: string
  label: string
  outcome_kind: 'absolute_return' | 'relative_return'
  horizon: V2Horizon
  lane: ResearchLaneV2
  primary_instrument: string
  comparison_instrument: string | null
}

export interface ResearchScopeV2 {
  target_id: string
  label: string
  horizon: V2Horizon
  instrument: string
  lane: 'shadow'
}

export interface ResearchProgramV2 {
  schema_version: 'forecast-loop.research-program/v2'
  program_id: string
  version: string
  market: string
  timezone: string
  calendar_id: string
  instruments: ResearchInstrumentV2[]
  decision_targets: ResearchTargetV2[]
  research_scopes: ResearchScopeV2[]
  content_hash?: string
  program_hash: string
}

export interface ForecastEvaluationV2 {
  actual_value?: number | null
  actual_label?: OutcomeLabel | null
  brier_score?: number | null
  baseline_brier_score?: number | null
  brier_improvement?: number | null
  direction_correct?: boolean | null
  evaluated_at?: string | null
}

export interface ForecastV2 {
  id: string
  run_id?: string
  target_id: string
  horizon: V2Horizon
  lane: ResearchLaneV2
  configured_lane?: ResearchLaneV2
  anchor_date: string
  target_date: string
  probabilities: Probabilities
  baseline_probabilities?: Probabilities
  threshold?: number
  neutral_threshold?: number
  rationale: string
  counter_evidence?: string[]
  invalidation_conditions?: string[]
  created_at: string
  evaluation?: ForecastEvaluationV2 | null
}

export interface LatestForecastsV2 {
  program_hash: string
  formal: ForecastV2 | null
  shadow: ForecastV2 | null
}

export type AgentScorecardAxisV2 =
  | 'final_system'
  | 'natural_horizon'
  | 'd1_impact'
  | 'reasoning'
  | 'incremental_value'
  | string

export interface AgentScorecardItemV2 {
  agent_id: string
  agent_name: string
  agent_version?: string
  model_name?: string
  prompt_version?: string
  target_id: string
  signal_kind: string
  horizon: V2Horizon
  sample_size: number
  independent_episodes: number
  average_brier: number | null
  baseline_brier: number | null
  brier_skill: number | null
  classwise_ece: Partial<Record<OutcomeLabel, number>> | null
  direction_accuracy: number | null
  reasoning_average: number | null
  ablation_brier_delta: number | null
  risk_diagnostics?: {
    critique_count: number
    counter_evidence_coverage_rate: number
    invalidation_coverage_rate: number
    risk_flag_rate: number
    evaluated_system_errors: number
    missed_risk_count: number
    missed_risk_rate: number | null
  } | null
  note: string
}

export interface AgentScorecardSectionV2 {
  axis: AgentScorecardAxisV2
  title: string
  items: AgentScorecardItemV2[]
}

export interface AgentScorecardsV2 {
  program_hash: string
  generated_at: string
  sections: AgentScorecardSectionV2[]
  premarket_history?: PremarketHistoryPoint[]
}

export interface PremarketHistoryPoint {
  forecast_hash: string
  forecast_session: string
  target_session: string
  predicted_direction: OutcomeLabel
  realized_return: number
  actual_label: OutcomeLabel
  direction_correct: boolean | null
  cumulative_sample_size: number
  cumulative_hits: number
  cumulative_win_rate: number | null
  rolling_20_win_rate: number | null
  long_only_period_return: number
  long_short_period_return: number
  long_only_cumulative_return: number
  long_short_cumulative_return: number
}

export interface ReasoningReviewV2 {
  id: string
  signal_id: string
  agent_id: string
  target_id: string
  signal_kind?: string
  horizon?: string
  status: string
  total_score: number | null
  human_review_required: boolean
  human_review_status: string | null
  deterministic_checks?: Record<string, boolean>
  rubric?: Record<string, unknown>
  created_at: string
}

export interface ReasoningReviewsV2 {
  items: ReasoningReviewV2[]
}

export type AgentTraceStatus = 'running' | 'completed' | 'failed' | 'degraded'
export type AgentWorkflowKind = 'prediction' | 'reflection' | 'agent_eval'
export type AgentEvalDecision = 'pending' | 'pass' | 'fail' | 'insufficient_sample'
export type AgentBadCaseStatus =
  | 'detected'
  | 'triaged'
  | 'confirmed'
  | 'materialized'
  | 'resolved'
  | 'rejected'

export interface AgentTraceReference {
  wiki_entry_id: string
  wiki_title: string
  wiki_version: string
  section: string
  content_hash: string
  evidence_item_id: string | null
  evidence_content_hash: string | null
  source_url: string | null
  published_at: string | null
}

export interface AgentTraceSpan {
  span_id: string
  parent_span_id: string | null
  node_id: string
  name: string
  span_kind: 'workflow' | 'agent' | 'llm' | 'validator' | 'persistence' | 'external'
  status: 'running' | 'completed' | 'failed'
  started_at: string
  completed_at: string | null
  duration_ms: number | null
  agent_id: string | null
  agent_version: string | null
  model_name: string | null
  prompt_version: string | null
  input_tokens: number | null
  output_tokens: number | null
  total_tokens: number | null
  estimated_cost_usd: number | null
  input_digest: string | null
  output_digest: string | null
  tool_name: string | null
  input_summary: string | null
  output_summary: string | null
  summary: string | null
  error_code: string | null
  error_summary: string | null
  attributes: Record<string, unknown>
  references: AgentTraceReference[]
}

export interface AgentTrace {
  id: string
  workflow_kind: AgentWorkflowKind
  subject_id: string
  mode: string
  status: AgentTraceStatus
  started_at: string
  completed_at: string | null
  duration_ms: number | null
  input_hash: string | null
  trace_policy_version: string
  telemetry_complete: boolean
  error_code: string | null
  error_summary: string | null
  attributes: Record<string, unknown>
  span_count: number
  spans: AgentTraceSpan[]
  external_url: string | null
  audit_url: string | null
  audit_label: string | null
  attempt_number?: number
  target_id?: string | null
  agent_id?: string | null
  horizon?: string | null
  natural_horizon?: V2Horizon | null
  sealed_at?: string | null
  artifact_links?: AgentTraceArtifactLink[]
}

export interface AgentTraceArtifactLink {
  id: string
  span_id: string | null
  artifact_kind: 'signal' | 'forecast' | 'evaluation' | 'reasoning_review' | 'reflection' | 'bad_case' | string
  artifact_id: string
  relation: 'input' | 'output' | 'reused' | 'diagnostic'
  content_hash: string | null
  created_at: string
}

export interface AgentTracePage {
  items: AgentTrace[]
  next_cursor?: string | null
  total?: number | null
}

export interface AgentObservabilitySummary {
  window_hours: number
  total_traces: number
  running_traces: number
  completed_traces: number
  failed_traces: number
  degraded_traces: number
  telemetry_complete_rate: number | null
  completion_rate: number | null
  p95_duration_ms: number | null
  by_workflow_kind: Record<string, number>
  recent: AgentTrace[]
  database_size_bytes?: number | null
  trace_storage_bytes?: number | null
  stored_span_count?: number
  stored_artifact_link_count?: number
  storage_warning_bytes?: number
  storage_warning?: boolean
}

export interface AgentEvalSuite {
  suite_id: string
  version: string
  title: string
  description: string
  synthetic: boolean
  runner_kind: string
  case_count: number
  target_ids: string[]
  arm_ids?: string[]
  content_hash: string
  source: 'public' | 'private'
}

export interface AgentEvalResult {
  id: string
  arm: 'baseline' | 'candidate'
  case_id: string
  evaluator_id: string
  evaluator_version: string
  metric_kind: string
  score: number | null
  passed: boolean | null
  status: 'passed' | 'failed' | 'not_applicable' | 'error'
  label: string | null
  explanation: string
  output_hash: string
  trace_id: string | null
  created_at: string
}

export interface AgentEvalTargetGateV2 {
  release_gate?: boolean | null
  decision?: AgentEvalDecision
  episode_count?: number
  hard_gates?: {
    schema_valid?: boolean | { rate?: number; passed?: boolean }
    cutoff_valid?: boolean | { rate?: number; passed?: boolean }
    citation_valid?: boolean | { rate?: number; passed?: boolean }
    trace_valid?: boolean | { rate?: number; passed?: boolean }
    must_pass_bad_case?: { rate?: number; passed?: boolean }
  }
  metric_gates?: {
    brier_delta?: number | null
    direction_drop?: number | null
    p95_latency_ratio?: number | null
    token_ratio?: number | null
    passed?: boolean | null
  }
  baseline?: Record<string, number | null>
  candidate?: Record<string, number | null>
  ablation?: Array<Record<string, unknown>>
  reasoning?: {
    baseline?: Record<string, number | null>
    candidate?: Record<string, number | null>
  }
}

export interface AgentEvalReportSummary {
  release_decision?: AgentEvalDecision
  case_count?: number
  outcome_case_count?: number
  must_pass_rate?: number
  hard_gate_pass?: boolean
  metric_gate_pass?: boolean | null
  metric_gates?: {
    brier_delta?: number | null
    direction_drop?: number | null
    p95_latency_ratio?: number | null
    token_ratio?: number | null
  }
  baseline?: Record<string, number | null>
  candidate?: Record<string, number | null>
  policy?: Record<string, number | string>
  pending_arms?: string[]
  pending_tasks?: string[]
  targets?: Record<string, AgentEvalTargetGateV2>
}

export interface AgentEvalExperiment {
  id: string
  suite_id: string
  suite_version: string
  suite_hash: string
  baseline_target_id: string
  baseline_target_hash: string
  candidate_target_id: string
  candidate_target_hash: string
  status: 'queued' | 'awaiting_draft' | 'ready_to_finalize' | 'running' | 'completed' | 'failed'
  release_decision: AgentEvalDecision
  policy_version: string
  created_at: string
  started_at: string | null
  completed_at: string | null
  report_hash: string | null
  error: string | null
  summary: AgentEvalReportSummary
  result_count: number
  results: AgentEvalResult[]
}

export interface AgentEvalCreateInput {
  suite_id: string
  suite_version?: string
  baseline_target_id: string
  candidate_target_id: string
  source: 'public' | 'private'
}

export interface AgentBadCaseEvent {
  id: string
  sequence_number: number
  event_type: string
  from_status: string | null
  to_status: string
  idempotency_key: string
  actor: string
  notes: string
  payload: Record<string, unknown>
  previous_event_hash: string | null
  content_hash: string
  occurred_at: string
}

export interface AgentBadCase {
  id: string
  trace_id: string
  span_id: string | null
  eval_result_id: string | null
  workflow_kind: string
  issue_type: string
  severity: 'low' | 'medium' | 'high' | 'critical'
  status: AgentBadCaseStatus
  title: string
  summary: string
  expected_behavior: string
  input_hash: string | null
  dedupe_hash: string
  dataset_id: string | null
  dataset_version: string | null
  created_at: string
  updated_at: string
  events: AgentBadCaseEvent[]
}

export interface AgentBadCaseTransitionInput {
  to_status: Exclude<AgentBadCaseStatus, 'detected'>
  actor: string
  notes?: string
  dataset_id?: string
  dataset_version?: string
  test_case?: Record<string, unknown>
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
