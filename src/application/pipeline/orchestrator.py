from typing import Optional

from src.application.pipeline.commands import (
    AnalyzeSessionCommand,
    GenerateDailySummaryCommand,
    SendWebhookCommand,
    SessionBuildCommand,
)
from src.application.pipeline.events import SessionAnalyzed, SessionSealed
from src.application.ports.task_dispatcher import TaskDispatcherPort


class PipelineOrchestrator:
    def __init__(self, dispatcher: TaskDispatcherPort):
        self.dispatcher = dispatcher

    def dispatch_session_build(self, command: SessionBuildCommand) -> Optional[str]:
        return self.dispatcher.dispatch_session_build(command)

    def dispatch_analyze_session(self, command: AnalyzeSessionCommand) -> Optional[str]:
        return self.dispatcher.dispatch_analyze_session(command)

    def dispatch_webhook(self, command: SendWebhookCommand) -> Optional[str]:
        return self.dispatcher.dispatch_webhook(command)

    def dispatch_generate_daily_summary(
        self,
        command: GenerateDailySummaryCommand,
    ) -> Optional[str]:
        return self.dispatcher.dispatch_generate_daily_summary(command)

    def on_session_sealed(self, event: SessionSealed) -> Optional[str]:
        return self.dispatcher.dispatch_analyze_session(
            AnalyzeSessionCommand(session_id=event.session_id, priority=event.priority)
        )

    def on_session_analyzed(self, event: SessionAnalyzed) -> None:
        _ = event
