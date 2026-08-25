"""Outbound webhook URL policy with DNS resolution at each delivery boundary."""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


class WebhookUrlPolicyError(ValueError):
    """Raised when a webhook destination is unsafe or unsupported."""


def _allowed_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    values = os.getenv("WEBHOOK_PRIVATE_NETWORK_ALLOWLIST", "").split(",")
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in values:
        candidate = value.strip()
        if candidate:
            networks.append(ipaddress.ip_network(candidate, strict=False))
    return tuple(networks)


def _is_allowed(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(address in network for network in _allowed_networks())


def _is_prohibited(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or address in ipaddress.ip_network("100.64.0.0/10")
    ) and not _is_allowed(address)


def validate_webhook_url(url: str) -> str:
    """Parse and resolve a destination; call again immediately before every delivery."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise WebhookUrlPolicyError("webhook URL must use http or https")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise WebhookUrlPolicyError("webhook URL must have a host and no credentials")
    try:
        resolved = socket.getaddrinfo(parsed.hostname, parsed.port or 0, type=socket.SOCK_STREAM)
    except (socket.gaierror, ValueError) as error:
        raise WebhookUrlPolicyError("webhook host cannot be resolved") from error
    if not resolved:
        raise WebhookUrlPolicyError("webhook host cannot be resolved")
    for entry in resolved:
        address = ipaddress.ip_address(entry[4][0])
        if _is_prohibited(address):
            raise WebhookUrlPolicyError("webhook host resolves to a prohibited network")
    return url
