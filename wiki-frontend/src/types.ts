export type HealthStatus = 'healthy' | 'watching' | 'challenged'
export type CausalStatus = 'verified' | 'supported' | 'hypothesis' | 'unresolved'
export type IssueType =
  | 'right_reason' | 'factual_error' | 'source_outdated' | 'scope_mismatch'
  | 'reasoning_failure' | 'transmission_mapping' | 'horizon_timing'
  | 'risk_plan_failure' | 'missing_invalidation' | 'other'

export interface WikiSection { id: string; heading: string; content: string }
export interface WikiEntry {
  id: string; title: string; version: string; contentHash: string; updatedAt: string
  summary: string; tags: string[]; citedByCount: number; sections: WikiSection[]
  sourceIds: string[]; sourceUrls: string[]
}
export interface WikiSource {
  id: string; origin: 'manual' | 'collector' | 'feedback'; title: string; filename: string
  media_type: string; source_url?: string | null; published_at?: string | null
  captured_at: string; ingested_at: string; byte_size: number; content_hash: string
}
export interface FeedbackEvent {
  id: string; origin: 'human' | 'reflection'; signal: 'support' | 'challenge'
  issue_type: IssueType; entry_id: string; entry_version: string; section: string
  claim_id?: string | null; reflection_id?: string | null; finding_id?: string | null
  target_date?: string | null; episode_key: string; index_code?: string | null
  agent_id?: string | null; causal_status: CausalStatus; attribution_confidence: number
  usage_strength: number; source_ids: string[]; summary: string; created_at: string
  content_hash: string
}
export interface FeedbackHealth {
  case_id: string; entry_id: string; entry_version: string; section: string
  claim_id?: string | null; status: HealthStatus; current_version: boolean
  posterior_reliability: number; support_weight: number; challenge_weight: number
  support_event_count: number; challenge_event_count: number
  independent_support_episodes: number; independent_challenge_episodes: number
  review_eligible: boolean; issue_counts: Record<string, number>; last_event_at?: string | null
}
export interface MaintenanceJob {
  id: string; status: 'awaiting_draft' | 'draft_ready' | 'published'; prepared_at: string
  source_ids: string[]; job_path: string; instructions_path: string
}
export interface Publication {
  job_id: string; change_id: string; operation: 'create' | 'update'; target_entry_id: string
  title: string; summary: string; proposed_version: string; source_ids: string[]
  status: 'staged' | 'published'; created_at: string; published_at?: string | null
}
export interface WorkbenchData {
  mode: string; entries: WikiEntry[]; sources: WikiSource[]; events: FeedbackEvent[]
  health: FeedbackHealth[]; jobs: MaintenanceJob[]; publications: Publication[]
}
