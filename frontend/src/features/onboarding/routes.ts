export function onboardingRouteByAction(action: string): string {
  if (action === 'configure_video_source') {
    return '/onboarding/basic/video'
  }
  if (action === 'configure_provider') {
    return '/onboarding/basic/provider'
  }
  if (action === 'configure_daily_summary') {
    return '/onboarding/basic/summary-time'
  }
  if (action === 'configure_home_profile') {
    return '/onboarding/personalize/profile'
  }
  if (action === 'configure_system_style' || action === 'configure_assistant_name') {
    return '/onboarding/personalize/style'
  }
  return '/onboarding'
}
