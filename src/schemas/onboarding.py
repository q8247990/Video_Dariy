from pydantic import BaseModel


class FlagStep(BaseModel):
    configured: bool


class VideoSourceStep(BaseModel):
    configured: bool
    validated: bool


class ProviderStep(BaseModel):
    configured: bool
    tested: bool


class CameraNotesStep(BaseModel):
    configured_count: int
    total_count: int


class OnboardingSteps(BaseModel):
    video_source: VideoSourceStep
    provider: ProviderStep
    daily_summary: FlagStep
    home_profile: FlagStep
    camera_notes: CameraNotesStep
    system_style: FlagStep
    assistant_name: FlagStep


class OnboardingStatusResponse(BaseModel):
    overall_status: str
    basic_ready: bool
    full_ready: bool
    steps: OnboardingSteps
    next_action: str
