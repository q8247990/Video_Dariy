from sqlalchemy.orm import Session

from src.application.pipeline.commands import (
    AnalyzeSessionCommand,
    GenerateDailySummaryCommand,
    SendWebhookCommand,
    SessionBuildCommand,
)
from src.application.ports.task_dispatcher import TaskDispatcherPort


class PipelineOrchestrator:
    def __init__(self, dispatcher: TaskDispatcherPort):
        self.dispatcher = dispatcher

    def dispatch_session_build(self, db: Session, command: SessionBuildCommand) -> str | None:
        return self.dispatcher.dispatch_session_build(db, command)

    def dispatch_analyze_session(self, db: Session, command: AnalyzeSessionCommand) -> str | None:
        return self.dispatcher.dispatch_analyze_session(db, command)

    def dispatch_webhook(self, db: Session, command: SendWebhookCommand) -> str | None:
        return self.dispatcher.dispatch_webhook(db, command)

    def dispatch_generate_daily_summary(
        self,
        db: Session,
        command: GenerateDailySummaryCommand,
    ) -> str | None:
        return self.dispatcher.dispatch_generate_daily_summary(db, command)
