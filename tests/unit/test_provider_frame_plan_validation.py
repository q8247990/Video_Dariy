"""Unit tests for image-mode frame-plan validation (Wave E / todo 25).

Covers the fail-fast contract: a provider created, updated or tested with an
image ``frame_sampling`` config must already be able to afford a representative
clip (60 s, 640x480, 800 prompt tokens) inside its ``max_model_len``. Video-mode
providers are never touched.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.application.bootstrap_fakes import FakeLLMGatewayFactory
from src.application.llm_providers import create_provider_use_case, update_provider_use_case
from src.application.llm_providers.frame_plan_validation import validate_image_frame_plan
from src.core.exceptions import DomainError
from src.core.provider_key_crypto import encrypt_provider_api_key
from src.services.frame_sampling.budget import FramePlan
from src.services.llm_provider_tester import ProviderTestResult, check_provider_connectivity


def _create_dump(extra_config: dict | None) -> dict:
    return {
        "provider_name": "vision-image",
        "api_base_url": "https://example.com/v1",
        "api_key": "dummy",
        "model_name": "m",
        "timeout_seconds": 30,
        "retry_count": 1,
        "enabled": True,
        "supports_vision": True,
        "supports_qa": False,
        "extra_config_json": extra_config,
    }


def _provider_stub(extra_config: dict | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        api_base_url="http://localhost:8000/v1",
        api_key=encrypt_provider_api_key("test-key"),
        model_name="test-model",
        timeout_seconds=30,
        extra_config_json=extra_config,
    )


# ---------------------------------------------------------------------------
# validate_image_frame_plan
# ---------------------------------------------------------------------------


def test_video_mode_skips_validation_even_with_invalid_context() -> None:
    # no frame_sampling at all -> video mode -> None, no error
    assert validate_image_frame_plan({"extra_config_json": {"max_model_len": 16}}) is None
    # explicit enabled: false -> video mode -> None, no error
    assert (
        validate_image_frame_plan(
            {"extra_config_json": {"frame_sampling": {"enabled": False}, "max_model_len": 0}}
        )
        is None
    )
    # no extra config at all -> video mode
    assert validate_image_frame_plan({"extra_config_json": None}) is None


def test_image_mode_requires_max_model_len() -> None:
    error = None
    try:
        validate_image_frame_plan({"extra_config_json": {"frame_sampling": {}}})
    except DomainError as exc:
        error = exc
    assert error is not None
    assert "max_model_len" in str(error)
    # max_model_len present but 0 is treated as missing
    error_zero = None
    try:
        validate_image_frame_plan({"extra_config_json": {"frame_sampling": {}, "max_model_len": 0}})
    except DomainError as exc:
        error_zero = exc
    assert error_zero is not None
    assert "max_model_len" in str(error_zero)


def test_image_mode_rejects_impossible_plan_with_remedies() -> None:
    error = None
    try:
        validate_image_frame_plan(
            {
                "extra_config_json": {
                    "max_model_len": 8192,
                    "frame_sampling": {"max_output_tokens": 8192, "min_frames": 8},
                }
            }
        )
    except DomainError as exc:
        error = exc
    assert error is not None
    message = str(error)
    # the three remedies
    assert "raise max_model_len" in message
    assert "lower max_output_tokens" in message
    assert "lower min_frames" in message
    # the computed numbers (1440p box bound: 3600 tokens/frame)
    assert "max_model_len=8192" in message
    assert "max_output_tokens=8192" in message
    assert "min_frames=8" in message
    assert "tokens_per_image=3600" in message
    assert "budget_tokens=-1620" in message


def test_legal_image_provider_returns_frame_plan() -> None:
    plan = validate_image_frame_plan(
        {"extra_config_json": {"max_model_len": 65536, "frame_sampling": {}}}
    )
    assert isinstance(plan, FramePlan)
    assert plan.frames > 0
    # 65536 ctx, 1440p box (3600 tokens/frame): budget 54086 // 3600 = 15
    # (>= min_frames 4 -> the representative plan still fits 65K)
    assert plan.frames == 15
    assert plan.tokens_per_image == 3600
    assert plan.clamp_reason == "budget"
    assert len(plan.timestamps) == plan.frames


def test_8k_context_provider_rejected_by_default_box() -> None:
    # 8K ctx with the default 1440p box: usable 7372, budget 7372-800-4096 = 2476,
    # 2476 // 3600 = 0 < min_frames 4 -> FramePlanImpossible -> business error
    error = None
    try:
        validate_image_frame_plan(
            {"extra_config_json": {"max_model_len": 8192, "frame_sampling": {}}}
        )
    except DomainError as exc:
        error = exc
    assert error is not None
    message = str(error)
    assert "tokens_per_image=3600" in message
    assert "budget_tokens=2476" in message
    assert "raise max_model_len" in message
    assert "lower max_output_tokens" in message
    assert "lower min_frames" in message


# ---------------------------------------------------------------------------
# use-case wiring (create / update)
# ---------------------------------------------------------------------------


def test_create_rejects_image_provider_without_max_model_len() -> None:
    result = create_provider_use_case(None, _create_dump({"frame_sampling": {}}), "zh-CN")
    assert result.error_code == 4001
    assert "max_model_len" in result.error_message


def test_create_accepts_legal_image_provider_config() -> None:
    # video-mode provider with a tiny context stays untouched by the plan check
    dump = _create_dump({"max_model_len": 16})
    dump["supports_vision"] = False
    dump["supports_qa"] = True
    dump["supports_tool_calling"] = True

    class _FakeDB:
        def add(self, provider: object) -> None:
            self.added = provider

        def flush(self) -> None:
            pass

    db = _FakeDB()  # type: ignore[assignment]
    result = create_provider_use_case(db, dump, "zh-CN")
    assert result.error_code == 0
    assert result.provider is not None


class _FakeQuery:
    def __init__(self, provider: object) -> None:
        self._provider = provider

    def filter(self, *args: object) -> _FakeQuery:
        return self

    def first(self) -> object:
        return self._provider


class _FakeDBForUpdate:
    def __init__(self, provider: object) -> None:
        self._provider = provider
        self.flushed = False

    def query(self, *args: object) -> _FakeQuery:
        return _FakeQuery(self._provider)

    def flush(self) -> None:
        self.flushed = True


def test_update_revalidates_video_to_image_with_impossible_context() -> None:
    provider = SimpleNamespace(
        id=1,
        supports_vision=True,
        supports_qa=False,
        supports_tool_calling=False,
        is_default_vision=False,
        is_default_qa=False,
        enabled=True,
        extra_config_json=None,  # video mode today
    )
    result = update_provider_use_case(
        _FakeDBForUpdate(provider),
        1,
        {
            "extra_config_json": {
                "max_model_len": 8192,
                "frame_sampling": {"max_output_tokens": 8192, "min_frames": 8},
            }
        },
        "zh-CN",
    )
    assert result.error_code == 4001
    assert "raise max_model_len" in result.error_message
    assert "lower max_output_tokens" in result.error_message
    assert "lower min_frames" in result.error_message
    assert _FakeDBForUpdate(provider).flushed is False


def test_update_leaves_video_provider_untouched() -> None:
    provider = SimpleNamespace(
        id=1,
        supports_vision=True,
        supports_qa=False,
        supports_tool_calling=False,
        is_default_vision=False,
        is_default_qa=False,
        enabled=True,
        extra_config_json=None,
        provider_type="vision",
    )
    db = _FakeDBForUpdate(provider)
    result = update_provider_use_case(db, 1, {"enabled": True}, "zh-CN")
    assert result.error_code == 0
    assert db.flushed is True


# ---------------------------------------------------------------------------
# provider TEST probe
# ---------------------------------------------------------------------------


def test_probe_fails_fast_when_image_plan_does_not_fit() -> None:
    provider = _provider_stub(
        {"max_model_len": 8192, "frame_sampling": {"max_output_tokens": 8192, "min_frames": 8}}
    )
    factory = FakeLLMGatewayFactory()

    result = check_provider_connectivity(provider, llm_factory=factory)

    assert isinstance(result, ProviderTestResult)
    assert result.success is False
    message = result.message
    assert "max_model_len" in message
    assert "max_output_tokens" in message
    assert "min_frames" in message
    # fail fast: no network round-trip, no gateway built
    assert factory.gateways == []


def test_probe_proceeds_when_image_plan_fits() -> None:
    provider = _provider_stub({"max_model_len": 65536, "frame_sampling": {}})
    factory = FakeLLMGatewayFactory()

    result = check_provider_connectivity(provider, llm_factory=factory)

    assert result.success is True
    assert len(factory.gateways) == 1
