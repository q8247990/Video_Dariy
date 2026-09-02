"""In-memory fakes for every port bound by the composition root.

These doubles live next to the composition root (``src/application/``)
because they implement the port ``Protocol`` shapes defined in
:mod:`src.application.ports`. They are intentionally simple:

* No I/O, no broker connections, no Redis.
* Each fake records every interaction so tests can assert on the call
  sequence without monkeypatching anything in ``src.infrastructure``.
* ``bootstrap_for_tests`` picks them as defaults; tests may override
  individual bindings with their own doubles.

The fakes must remain dependency-free — they do not import
``src.infrastructure.*`` or any Celery objects, which keeps
``tests/unit/test_application_bootstrap.py`` hermetic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from src.application.ports.clock import ClockPort, IdGeneratorPort
from src.application.ports.llm_gateway import LLMGatewayFactoryPort, LLMGatewayPort
from src.application.ports.task_control import HeartbeatReport, TaskControlPort
from src.application.ports.task_dispatcher import TaskDispatcherPort

# ---------------------------------------------------------------------------
# TaskDispatcherPort fake
# ---------------------------------------------------------------------------


_UNSET = object()


class FakeTaskDispatcher(TaskDispatcherPort):
    """Records dispatch calls and returns deterministic Celery task IDs.

    The task IDs are derived from a monotonic counter so tests can assert
    ``dispatched[-1] == "fake-task-3"`` without depending on the
    underlying UUID entropy. ``set_next_return`` lets a test simulate
    the production semantics where the dispatcher declines to enqueue
    (``None`` return) on dedupe supersession.
    """

    def __init__(self, *, task_id_prefix: str = "fake-task") -> None:
        self._counter = 0
        self._prefix = task_id_prefix
        self.dispatched_session_build: list[Any] = []
        self.dispatched_analyze_session: list[Any] = []
        self.dispatched_daily_summary: list[Any] = []
        self.dispatched_webhook: list[Any] = []
        # Sentinel distinguishes "no scripted return" (counter fallback)
        # from "scripted return of None" (dispatcher declined to enqueue).
        self._next_return: Any = _UNSET

    def _next_id(self) -> str:
        self._counter += 1
        return f"{self._prefix}-{self._counter}"

    def set_next_return(self, value: Optional[str]) -> None:
        """Force the next dispatch call to return ``value`` (consumed once).

        ``None`` here means "dispatcher declined to enqueue" — exactly
        the production meaning. Pass ``"some-id"`` to mimic a Celery
        ``task_id`` reply.
        """

        self._next_return = value

    # ------------------------------------------------------------------
    # TaskDispatcherPort protocol
    # ------------------------------------------------------------------

    def dispatch_session_build(self, command: Any) -> Optional[str]:
        self.dispatched_session_build.append(command)
        return self._dispatch_or_scripted()

    def dispatch_analyze_session(self, command: Any) -> Optional[str]:
        self.dispatched_analyze_session.append(command)
        return self._dispatch_or_scripted()

    def dispatch_generate_daily_summary(self, command: Any) -> Optional[str]:
        self.dispatched_daily_summary.append(command)
        return self._dispatch_or_scripted()

    def dispatch_webhook(self, command: Any) -> Optional[str]:
        self.dispatched_webhook.append(command)
        return self._dispatch_or_scripted()

    def _dispatch_or_scripted(self) -> Optional[str]:
        if self._next_return is not _UNSET:
            value: Optional[str] = self._next_return
            self._next_return = _UNSET
            return value
        return self._next_id()

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    @property
    def total_dispatched(self) -> int:
        """Total number of dispatch calls recorded across all command types."""

        return (
            len(self.dispatched_session_build)
            + len(self.dispatched_analyze_session)
            + len(self.dispatched_daily_summary)
            + len(self.dispatched_webhook)
        )

    def reset(self) -> None:
        """Clear all recorded dispatches and reset the task-ID counter."""

        self.dispatched_session_build.clear()
        self.dispatched_analyze_session.clear()
        self.dispatched_daily_summary.clear()
        self.dispatched_webhook.clear()
        self._counter = 0
        self._next_return = _UNSET


# ---------------------------------------------------------------------------
# ClockPort fake
# ---------------------------------------------------------------------------


class FakeClock(ClockPort):
    """Deterministic clock for tests.

    Initial value defaults to a fixed ``epoch_utc`` so tests can assert on
    exact timestamps without worrying about the system clock. ``tick``
    advances the clock by a ``timedelta`` (default 1 second) and
    ``set_now`` lets a test rewind or freeze at an arbitrary instant.
    """

    epoch_utc: datetime = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    def __init__(self, *, initial: Optional[datetime] = None) -> None:
        if initial is None:
            initial = self.epoch_utc
        elif initial.tzinfo is None:
            raise ValueError("FakeClock initial instant must be timezone-aware UTC")
        self._now: datetime = initial
        self.ticks: list[timedelta] = []

    def now(self) -> datetime:
        return self._now

    def tick(self, *, by: timedelta = timedelta(seconds=1)) -> datetime:
        self.ticks.append(by)
        self._now = self._now + by
        return self._now

    def set_now(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FakeClock.set_now requires a timezone-aware instant")
        self._now = instant


# ---------------------------------------------------------------------------
# IdGeneratorPort fake
# ---------------------------------------------------------------------------


class FakeIdGenerator(IdGeneratorPort):
    """Returns monotonic, predictable IDs by default.

    Tests that need real UUID-shape strings can opt into ``use_uuid=True``
    (still deterministic per-test when ``seed`` is supplied). The default
    counter mode makes assertions like
    ``container.id_gen.new_id() == "fake-id-3"`` trivial.
    """

    def __init__(
        self,
        *,
        prefix: str = "fake-id",
        use_uuid: bool = False,
        seed: Optional[int] = None,
    ) -> None:
        self._prefix = prefix
        self._counter = 0
        self._use_uuid = use_uuid
        self._uuid_seed = seed
        self.issued: list[str] = []

    def new_id(self) -> str:
        self._counter += 1
        if self._use_uuid:
            value = str(uuid4())
        else:
            value = f"{self._prefix}-{self._counter}"
        self.issued.append(value)
        return value


# ---------------------------------------------------------------------------
# TaskControlPort fake
# ---------------------------------------------------------------------------


class FakeTaskControl(TaskControlPort):
    """Records revoke / heartbeat calls without touching Celery.

    ``revoke`` simply appends the ``(task_id, terminate)`` tuple to
    ``revocations``. ``heartbeat`` returns a ``HeartbeatReport``
    populated from configurable defaults — useful for asserting that an
    endpoint actually invokes the port before reporting ``healthy``.
    """

    def __init__(
        self,
        *,
        default_worker_count: int = 1,
        default_active_count: int = 0,
    ) -> None:
        self.revocations: list[tuple[str, bool]] = []
        self.heartbeats: list[str] = []
        self._default_worker_count = default_worker_count
        self._default_active_count = default_active_count

    def revoke(self, task_id: str, *, terminate: bool = False) -> None:
        self.revocations.append((task_id, terminate))

    def heartbeat(self, queue: str = "") -> HeartbeatReport:
        self.heartbeats.append(queue)
        return HeartbeatReport(
            queue=queue,
            worker_count=self._default_worker_count,
            active_count=self._default_active_count,
        )


# ---------------------------------------------------------------------------
# LLMGatewayFactoryPort / LLMGatewayPort fakes
# ---------------------------------------------------------------------------


class FakeLLMGateway(LLMGatewayPort):
    """In-memory LLM gateway returning scripted replies.

    The default reply set is empty, so ``chat_completion`` returns
    ``None`` and ``chat_completion_with_tools`` returns ``(None, None)``
    — exactly what the production gateway returns when the model emits
    no content. Tests that need richer behaviour set ``replies`` or
    supply a custom ``completion_callable``.
    """

    supports_tool_calling: bool = False

    def __init__(
        self,
        *,
        model_name: str = "fake-model",
        supports_tool_calling: bool = False,
    ) -> None:
        self.model_name = model_name
        self.supports_tool_calling = supports_tool_calling
        self.replies: list[str] = []
        self.completion_calls: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.last_usage: Optional[dict[str, int]] = None
        self.last_raw_response_text: Optional[str] = None
        self._closed = False

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        response_format: Optional[dict[str, Any]] = None,
        extra_body: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        self.completion_calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
                "extra_body": extra_body,
            }
        )
        if self.replies:
            return self.replies.pop(0)
        return None

    def chat_completion_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> tuple[Optional[str], Optional[list[dict[str, Any]]]]:
        self.tool_calls.append({"messages": messages, "tools": tools})
        return (None, None)

    def get_last_usage(self) -> Optional[dict[str, int]]:
        return self.last_usage

    def get_last_raw_response_text(self) -> Optional[str]:
        return self.last_raw_response_text

    def close(self) -> None:
        self._closed = True

    def probe_vision(self) -> bool:
        return False

    def probe_tool_calling(self) -> bool:
        return self.supports_tool_calling


class FakeLLMGatewayFactory(LLMGatewayFactoryPort):
    """Builds fresh :class:`FakeLLMGateway` instances and remembers them.

    Production never caches a gateway inside the factory (each provider
    has its own HTTP client with bespoke timeout / API key). The fake
    mirrors that: every call to ``build`` returns a new
    :class:`FakeLLMGateway`, and the factory keeps a list so tests can
    introspect what was constructed.
    """

    def __init__(
        self,
        *,
        supports_tool_calling: bool = False,
    ) -> None:
        self.supports_tool_calling = supports_tool_calling
        self.gateways: list[FakeLLMGateway] = []
        self.build_calls: list[dict[str, Any]] = []

    def build(
        self,
        *,
        api_base_url: str,
        api_key: str,
        model_name: str,
        timeout_seconds: int,
        supports_tool_calling: bool = False,
    ) -> LLMGatewayPort:
        gateway = FakeLLMGateway(
            model_name=model_name,
            supports_tool_calling=supports_tool_calling or self.supports_tool_calling,
        )
        self.build_calls.append(
            {
                "api_base_url": api_base_url,
                "api_key": api_key,
                "model_name": model_name,
                "timeout_seconds": timeout_seconds,
                "supports_tool_calling": gateway.supports_tool_calling,
            }
        )
        self.gateways.append(gateway)
        return gateway


__all__ = [
    "FakeClock",
    "FakeIdGenerator",
    "FakeLLMGateway",
    "FakeLLMGatewayFactory",
    "FakeTaskControl",
    "FakeTaskDispatcher",
]
