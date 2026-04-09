from src.core.i18n import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    normalize_locale,
    reload_catalogs,
    t,
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


def test_t_both_locales_have_same_keys():
    reload_catalogs()
    from src.core.i18n import _get_catalog

    zh = _get_catalog("zh-CN")
    en = _get_catalog("en-US")
    zh_keys = set(zh.keys())
    en_keys = set(en.keys())
    assert (
        zh_keys == en_keys
    ), f"Missing in en-US: {zh_keys - en_keys}, Missing in zh-CN: {en_keys - zh_keys}"


def test_t_en_us_fallback_to_zh_cn_for_missing_key():
    from src.core.i18n import _catalogs

    reload_catalogs()
    from src.core.i18n import _get_catalog

    _get_catalog("en-US")
    _catalogs["en-US"]["_test_only_en"] = "english only"
    assert t("_test_only_en", "en-US") == "english only"
    assert t("_test_only_en", "zh-CN") == "_test_only_en"
