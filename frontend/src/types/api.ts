export type Pagination = {
  page: number
  page_size: number
  total: number
}

export type PaginatedData<T> = {
  list: T[]
  pagination: Pagination
}

export type User = {
  id: number
  username: string
}

export type LoginResponse = {
  token: string
  user: User
}

export type VideoSource = {
  id: number
  source_name: string
  camera_name: string
  location_name: string
  description: string | null
  prompt_text: string | null
  source_type: string
  config_json: Record<string, unknown> | null
  enabled: boolean
  source_paused: boolean
  paused_at: string | null
  last_scan_at: string | null
  last_validate_status: string | null
  last_validate_message: string | null
  last_validate_at: string | null
  created_at: string
  updated_at: string
}

export type VideoSourceCreate = {
  source_name: string
  camera_name: string
  location_name: string
  description: string
  prompt_text: string
  source_type: string
  config_json: Record<string, unknown>
  enabled: boolean
}

export type VideoSourceStatus = {
  source_id: number
  video_earliest_time: string | null
  video_latest_time: string | null
  analyzed_earliest_time: string | null
  analyzed_latest_time: string | null
  analyzed_coverage_percent: number | null
  analysis_state: 'analyzing' | 'paused' | 'stopped' | string
  minutes_since_last_new_video: number | null
  full_build_running: boolean
  updated_at: string
}

export type Provider = {
  id: number
  provider_name: string
  api_base_url: string
  model_name: string
  timeout_seconds: number
  retry_count: number
  extra_config_json: Record<string, unknown> | null
  enabled: boolean
  supports_vision: boolean
  supports_qa: boolean
  supports_tool_calling: boolean
  is_default_vision: boolean
  is_default_qa: boolean
  availability_status: 'available' | 'degraded' | 'unavailable' | 'unknown' | string
  availability_message: string
  last_test_status: string | null
  last_test_message: string | null
  last_test_at: string | null
  created_at: string
  updated_at: string
}

export type ProviderUsageProviderItem = {
  provider_id: number | null
  provider_name: string
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

export type ProviderUsageDailyItem = {
  date: string
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  providers: ProviderUsageProviderItem[]
}

export type ProviderCreate = {
  provider_name: string
  api_base_url: string
  api_key: string
  model_name: string
  timeout_seconds: number
  retry_count: number
  extra_config_json: Record<string, unknown>
  enabled: boolean
  supports_vision: boolean
  supports_qa: boolean
  supports_tool_calling: boolean
  is_default_vision: boolean
  is_default_qa: boolean
}

export type ProviderUpdate = {
  provider_name?: string
  api_base_url?: string
  api_key?: string
  model_name?: string
  timeout_seconds?: number
  retry_count?: number
  extra_config_json?: Record<string, unknown>
  enabled?: boolean
  supports_vision?: boolean
  supports_qa?: boolean
  supports_tool_calling?: boolean
  is_default_vision?: boolean
  is_default_qa?: boolean
}

export type EventRecord = {
  id: number
  source_id: number
  session_id: number
  event_start_time: string
  event_end_time: string | null
  session_start_time: string | null
  session_total_duration_seconds: number | null
  session_analysis_status: string | null
  object_type: string | null
  action_type: string | null
  event_type: string | null
  title: string | null
  summary: string | null
  detail: string | null
  importance_level: 'low' | 'medium' | 'high' | null
  offset_start_sec: number | null
  offset_end_sec: number | null
  related_entities_json: Record<string, unknown>[] | null
  observed_actions_json: string[] | null
  interpreted_state_json: string[] | null
  description: string
  confidence_score: number | null
  raw_result: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export type EventDetail = EventRecord & {
  source_name: string
  camera_name: string
  location_name: string
  tags: { id: number; tag_name: string; tag_type: string; description: string | null; enabled: boolean }[] | null
}

export type DashboardAction = {
  label: string
  target: string
}

export type DashboardSystemStatusItem = {
  key: string
  label: string
  status: 'ok' | 'partial' | 'not_ready' | 'error'
}

export type DashboardSystemStatus = {
  overall_status: 'basic_not_ready' | 'basic_ready' | 'full_ready'
  title: string
  description: string
  items: DashboardSystemStatusItem[]
  primary_action: DashboardAction
  detail_action: DashboardAction
}

export type DashboardAlert = {
  show: boolean
  type: string | null
  title: string | null
  description: string | null
  action: DashboardAction | null
}

export type DashboardTaskSummary = {
  last_scan_at: string | null
  last_analysis_status: string | null
  last_daily_summary_status: string | null
  failed_task_count_24h: number
}

export type DashboardEventSummary = {
  today_event_count: number
  yesterday_event_count: number
  important_event_count_24h: number
}

export type DashboardLatestDailySummary = {
  exists: boolean
  date: string | null
  status: string
  summary_preview: string | null
  empty_reason: string | null
}

export type DashboardImportantEvent = {
  id: number
  title: string
  summary: string
  event_time: string
  camera_name: string
}

export type DashboardOverview = {
  assistant_name: string
  system_status: DashboardSystemStatus
  alert: DashboardAlert
  task_summary: DashboardTaskSummary
  event_summary: DashboardEventSummary
  latest_daily_summary: DashboardLatestDailySummary
  important_events: DashboardImportantEvent[]
}

export type ChatReferenceEvent = {
  id: number
  description: string
}

export type ChatAskResponse = {
  question: string
  answer_text: string
  referenced_events: ChatReferenceEvent[] | null
  referenced_sessions?: Record<string, unknown>[] | null
}

export type ChatHistoryItem = {
  id: number
  user_question: string
  parsed_condition_json: Record<string, unknown> | null
  answer_text: string
  referenced_event_ids_json: number[] | null
  provider_id: number | null
  created_at: string
}

export type WebhookConfig = {
  id: number
  name: string
  url: string
  headers_json: Record<string, string> | null
  event_subscriptions_json: WebhookSubscriptionRule[] | null
  enabled: boolean
  created_at: string
  updated_at: string
}

export type WebhookSubscriptionRule = {
  event: string
  version: string
}

export type WebhookCreate = {
  name: string
  url: string
  headers_json: Record<string, string>
  event_subscriptions_json: WebhookSubscriptionRule[]
  enabled: boolean
}

export type WebhookUpdate = {
  name?: string
  url?: string
  headers_json?: Record<string, string>
  event_subscriptions_json?: WebhookSubscriptionRule[]
  enabled?: boolean
}

export type VideoSession = {
  id: number
  source_id: number
  session_start_time: string
  session_end_time: string
  total_duration_seconds: number | null
  merge_rule: string | null
  analysis_status: string
  summary_text: string | null
  activity_level: 'low' | 'medium' | 'high' | null
  main_subjects_json: string[] | null
  has_important_event: boolean | null
  analysis_notes_json: { type: string; note: string }[] | null
  last_analyzed_at: string | null
  created_at: string
  updated_at: string
}

export type SessionPlaybackFile = {
  file_id: number
  file_name: string
  stream_url: string
  sort_index: number
}

export type SessionPlayback = {
  session_id: number
  session_start_time: string
  session_end_time: string
  playback_url: string
  files: SessionPlaybackFile[]
}

export type DailySummary = {
  id: number
  summary_date: string
  summary_title: string | null
  overall_summary: string | null
  detail_text: string | null
  subject_sections_json: DailySummarySubjectSection[] | null
  attention_items_json: DailySummaryAttentionItem[] | null
  event_count: number
  provider_id: number | null
  generated_at: string
}

export type DailySummarySubjectSection = {
  subject_name: string
  subject_type: 'member' | 'pet'
  summary: string
  attention_needed: boolean
  activity_score?: number
}

export type DailySummaryAttentionItem = {
  title: string
  summary: string
  level: 'low' | 'medium' | 'high' | string
}

export type DailySummaryDetail = DailySummary

export type SystemConfig = {
  daily_summary_schedule?: string
  scan_interval_seconds?: number
  scan_hot_window_hours?: number
  scan_late_tolerance_seconds?: number
  latency_alert_threshold_seconds?: number
  alert_consecutive_required?: number
  alert_notify_cooldown_minutes?: number
  default_session_merge_gap_seconds?: number
  tag_recommendation_enabled?: boolean
  mcp_enabled?: boolean
  mcp_token?: string
}

export type TaskLogItem = {
  id: number
  task_type: string
  task_target_id: number | null
  status: string
  queue_task_id: string | null
  cancel_requested: boolean
  started_at: string | null
  finished_at: string | null
  retry_count: number
  message: string | null
  detail_json: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export type HomeProfile = {
  id: number
  home_name: string
  family_tags: string[]
  focus_points: string[]
  system_style: string
  style_preference_text: string | null
  assistant_name: string
  home_note: string | null
  created_at: string
  updated_at: string
}

export type HomeProfilePayload = {
  home_name: string
  family_tags: string[]
  focus_points: string[]
  system_style: string
  style_preference_text: string
  assistant_name: string
  home_note: string
}

export type HomeEntity = {
  id: number
  entity_type: 'member' | 'pet'
  name: string
  role_type: string
  age_group: string | null
  breed: string | null
  appearance_desc: string | null
  personality_desc: string | null
  note: string | null
  sort_order: number
  is_enabled: boolean
  created_at: string
  updated_at: string
}

export type MemberPayload = {
  name: string
  role_type: string
  age_group?: string
  appearance_desc?: string
  note?: string
  sort_order: number
  is_enabled: boolean
}

export type PetPayload = {
  name: string
  role_type: string
  breed?: string
  appearance_desc?: string
  personality_desc?: string
  note?: string
  sort_order: number
  is_enabled: boolean
}

export type HomeOptions = {
  family_tags: string[]
  focus_points: string[]
  system_styles: string[]
  entity_types: string[]
  member_roles: string[]
  pet_roles: string[]
  age_groups: string[]
}

export type VideoPathValidateRequest = {
  path: string
}

export type VideoPathValidateResponse = {
  valid: boolean
  file_count: number
  latest_file_time: string | null
  earliest_file_time: string | null
  message: string
}

export type OnboardingStepFlag = {
  configured: boolean
}

export type OnboardingVideoStep = {
  configured: boolean
  validated: boolean
}

export type OnboardingProviderStep = {
  configured: boolean
  tested: boolean
}

export type OnboardingCameraNotesStep = {
  configured_count: number
  total_count: number
}

export type OnboardingSteps = {
  video_source: OnboardingVideoStep
  provider: OnboardingProviderStep
  daily_summary: OnboardingStepFlag
  home_profile: OnboardingStepFlag
  camera_notes: OnboardingCameraNotesStep
  system_style: OnboardingStepFlag
  assistant_name: OnboardingStepFlag
}

export type OnboardingStatus = {
  overall_status: 'basic_not_ready' | 'basic_ready' | 'full_ready'
  basic_ready: boolean
  full_ready: boolean
  steps: OnboardingSteps
  next_action: string
}
