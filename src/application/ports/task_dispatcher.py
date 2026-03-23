from typing import Optional, Protocol

from src.application.pipeline.commands import (
    AnalyzeSessionCommand,
    GenerateDailySummaryCommand,
    SendWebhookCommand,
    SessionBuildCommand,
)


class TaskDispatcherPort(Protocol):
    def dispatch_session_build(self, command: SessionBuildCommand) -> Optional[str]: ...

    def dispatch_analyze_session(self, command: AnalyzeSessionCommand) -> Optional[str]: ...

    def dispatch_generate_daily_summary(
        self, command: GenerateDailySummaryCommand
    ) -> Optional[str]: ...

    def dispatch_webhook(self, command: SendWebhookCommand) -> Optional[str]: ...
