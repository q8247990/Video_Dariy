from fastapi import APIRouter

from src.api.v1.endpoints import (
    auth,
    chat,
    daily_summaries,
    dashboard,
    events,
    home_profile,
    llm_providers,
    media,
    onboarding,
    sessions,
    system_config,
    tags,
    tasks,
    video_sources,
    webhooks,
)

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
api_router.include_router(video_sources.router, prefix="/video-sources", tags=["video-sources"])
api_router.include_router(llm_providers.router, prefix="/providers", tags=["providers"])
api_router.include_router(system_config.router, prefix="/system-config", tags=["system-config"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(events.router, prefix="/events", tags=["events"])
api_router.include_router(home_profile.router, prefix="/home-profile", tags=["home-profile"])
api_router.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
api_router.include_router(
    daily_summaries.router, prefix="/daily-summaries", tags=["daily-summaries"]
)
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(tags.router, prefix="/tags", tags=["tags"])
api_router.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
api_router.include_router(onboarding.router, prefix="/onboarding", tags=["onboarding"])

# Mount media slightly outside standard REST if we want, but prefixing here is fine
api_router.include_router(media.router, prefix="/media", tags=["media"])
