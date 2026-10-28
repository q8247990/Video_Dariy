import type { SystemConfig } from '../types/api'

export const DEFAULT_HOME_TIMEZONE = 'Asia/Shanghai'
export const DEFAULT_DAILY_SUMMARY_SCHEDULE = '10:00'
export const DEFAULT_SCAN_INTERVAL_SECONDS = 300
export const DEFAULT_SCAN_HOT_WINDOW_HOURS = 24
export const DEFAULT_MEDIA_TTL_SECONDS = 1800

export const DEFAULT_SYSTEM_CONFIG: SystemConfig = {
  home_timezone: DEFAULT_HOME_TIMEZONE,
  daily_summary_schedule: DEFAULT_DAILY_SUMMARY_SCHEDULE,
  scan_interval_seconds: DEFAULT_SCAN_INTERVAL_SECONDS,
  scan_hot_window_hours: DEFAULT_SCAN_HOT_WINDOW_HOURS,
  tag_recommendation_enabled: false,
  mcp_enabled: false,
  media_manifest_ttl_seconds: DEFAULT_MEDIA_TTL_SECONDS,
  media_segment_ttl_seconds: DEFAULT_MEDIA_TTL_SECONDS,
  media_entity_image_ttl_seconds: DEFAULT_MEDIA_TTL_SECONDS,
}
