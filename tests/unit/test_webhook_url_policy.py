import socket

import pytest

from src.services.webhook_url_policy import WebhookUrlPolicyError, validate_webhook_url


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/webhook",
        "http://127.0.0.1/webhook",
        "http://[::1]/webhook",
        "http://10.0.0.1/webhook",
        "http://172.16.0.1/webhook",
        "http://192.168.1.1/webhook",
        "http://169.254.1.1/webhook",
        "http://0.0.0.0/webhook",
        "http://100.64.0.1/webhook",
        "ftp://example.com/webhook",
        "https://user:password@example.com/webhook",
    ],
)
def test_validate_webhook_url_rejects_prohibited_target(url: str) -> None:
    with pytest.raises(WebhookUrlPolicyError):
        validate_webhook_url(url)


def test_validate_webhook_url_allows_public_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, 1, 6, "", ("8.8.8.8", 0))],
    )

    assert (
        validate_webhook_url("https://public.example/webhook") == "https://public.example/webhook"
    )


def test_validate_webhook_url_rechecks_dns_at_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(["8.8.8.8", "127.0.0.1"])
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, 1, 6, "", (next(responses), 0))],
    )

    validate_webhook_url("https://rebind.example/webhook")
    with pytest.raises(WebhookUrlPolicyError):
        validate_webhook_url("https://rebind.example/webhook")
