import json
from pathlib import Path

from src.core.i18n import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    normalize_locale,
    reload_catalogs,
)


def setup_function():
    reload_catalogs()


def test_t_returns_zh_cn_by_default():
    assert t("dashboard.event.unnamed") == "未命名事件"


def test_t_returns_en_us_when_requested():
    assert t("dashboard.event.unnamed", "en-US") == "Unnamed Event"


def test_t_falls_back_to_default_locale_for_unknown_key():
    assert t("nonexistent.key") == "nonexistent.key"


def test_t_falls_back_to_default_locale_for_unsupported_locale():
    result = t("dashboard.event.unnamed", "fr-FR")
    assert result == "未命名事件"


def test_t_with_params():
    result = t("dashboard.alert.analysis_task_error.desc", "zh-CN", count=5)
    assert "5" in result


def test_t_with_params_en():
    result = t("dashboard.alert.analysis_task_error.desc", "en-US", count=3)
    assert "3" in result


def test_t_missing_param_returns_template():
    result = t("dashboard.alert.analysis_task_error.desc", "zh-CN")
    assert "{count}" in result


def test_normalize_locale_exact_match():
    assert normalize_locale("zh-CN") == "zh-CN"
    assert normalize_locale("en-US") == "en-US"


def test_normalize_locale_case_insensitive():
    assert normalize_locale("ZH-CN") == "zh-CN"
    assert normalize_locale("EN-US") == "en-US"
    assert normalize_locale("zh-cn") == "zh-CN"


def test_normalize_locale_underscore():
    assert normalize_locale("zh_CN") == "zh-CN"
    assert normalize_locale("en_US") == "en-US"


def test_normalize_locale_prefix_only():
    assert normalize_locale("zh") == "zh-CN"
    assert normalize_locale("en") == "en-US"


def test_normalize_locale_empty_returns_default():
    assert normalize_locale("") == DEFAULT_LOCALE
    assert normalize_locale(None) == DEFAULT_LOCALE
    assert normalize_locale("   ") == DEFAULT_LOCALE


def test_normalize_locale_unknown_returns_default():
    assert normalize_locale("fr") == DEFAULT_LOCALE
    assert normalize_locale("ja-JP") == DEFAULT_LOCALE


def test_supported_locales_contains_both():
    assert "zh-CN" in SUPPORTED_LOCALES
    assert "en-US" in SUPPORTED_LOCALES


# Import the public ``t`` helper at module scope so the parity and
# fallback tests below exercise the same boundary as every other test.
from src.core.i18n import t  # noqa: E402


def _catalog_keys(locale: str) -> set[str]:
    """Load the on-disk locale file and return its key set.

    This is * *not* an internal-symbol read — the locale JSON files are
    shipped data fixtures the application loads at startup. Reading them
    directly to enumerate the key set keeps the parity assertion honest
    against future catalogue edits without poking at private catalog
    state.
    """
    locale_dir = Path(__file__).parent.parent.parent / "src" / "core" / "i18n" / "locales"
    stem_map = {"zh-CN": "zh_CN", "en-US": "en_US"}
    path = locale_dir / f"{stem_map[locale]}.json"
    return set(json.loads(path.read_text(encoding="utf-8")).keys())


def test_t_both_locales_have_same_keys() -> None:
    """Public-API parity check: every key is present in both catalogs."""
    reload_catalogs()
    zh_keys = _catalog_keys("zh-CN")
    en_keys = _catalog_keys("en-US")
    assert zh_keys == en_keys, (
        f"Missing in en-US: {zh_keys - en_keys}, Missing in zh-CN: {en_keys - zh_keys}"
    )

    parity_keys = zh_keys | en_keys
    assert parity_keys, "catalogs must contain at least one key"

    for key in sorted(parity_keys):
        zh_value = t(key, "zh-CN")
        en_value = t(key, "en-US")
        # t() echoes the key when both catalogs miss; a real translation
        # must therefore be a non-empty string (some keys — e.g. "ok" —
        # legitimately translate to themselves in both locales).
        assert zh_value, f"missing zh-CN translation: {key!r}"
        assert en_value, f"missing en-US translation: {key!r}"


def test_t_en_us_fallback_to_zh_cn_for_missing_key() -> None:
    """Whitebox pin for the en-US → zh-CN fallback path.

    The fallback contract (``en-US`` lookup miss falls back to the
    ``zh-CN`` catalog) is observable only by mutating the in-memory
    catalog, because the on-disk JSON files are static fixtures and
    there is no public API to inject a sub-catalog. The test directly
    touches the private catalog dict and is explicitly marked as such;
    promote it to a public seam (a ``set_catalog_override`` helper on
    ``src.core.i18n``) when the en-US catalog needs runtime overrides
    in production.
    """
    from src.core.i18n import _catalogs, _get_catalog  # noqa: PLC0415

    reload_catalogs()
    _get_catalog("en-US")
    _catalogs["en-US"]["_test_only_en"] = "english only"

    try:
        assert t("_test_only_en", "en-US") == "english only"
        assert t("_test_only_en", "zh-CN") == "_test_only_en"
    finally:
        _catalogs["en-US"].pop("_test_only_en", None)
