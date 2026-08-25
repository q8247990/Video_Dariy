"""Short-lived, resource-bound capabilities for browser media requests."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlencode

from src.core.config import settings

MediaResourceKind = Literal["file", "session_stream", "session_hls", "image"]

SIGNER_VERSION: Final = "v1"
MANIFEST_TTL_SECONDS: Final = 1_800
SEGMENT_TTL_SECONDS: Final = 1_800


@dataclass(frozen=True, slots=True)
class MediaCapability:
    """The signed, filesystem-independent authorization scope for one media request."""

    resource_kind: MediaResourceKind
    resource_id: int
    method: str
    expires_at: int
    session_parent_id: int | None = None
    signer_version: str = SIGNER_VERSION

    def canonical_payload(self) -> bytes:
        """Return the exact stable byte sequence authenticated by HMAC."""
        parent = "" if self.session_parent_id is None else str(self.session_parent_id)
        return ":".join(
            (
                self.signer_version,
                self.method.upper(),
                self.resource_kind,
                str(self.resource_id),
                str(self.expires_at),
                parent,
            )
        ).encode("ascii")


class MediaCapabilityError(ValueError):
    """Raised when a media capability cannot authorize the requested resource."""


class MediaSigningService:
    """Signs and verifies versioned HMAC-SHA256 media capabilities."""

    def __init__(self, signing_key: str) -> None:
        self._signing_key = signing_key.encode("utf-8")

    @classmethod
    def from_settings(cls) -> MediaSigningService:
        """Build the application service from the dedicated media signing secret."""
        return cls(settings.MEDIA_SIGNING_KEY)

    def issue(self, capability: MediaCapability) -> str:
        """Return an opaque URL-safe capability token without filesystem data."""
        payload = _encode(capability.canonical_payload())
        signature = _encode(
            hmac.new(self._signing_key, payload.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{capability.signer_version}.{payload}.{signature}"

    def verify(
        self, token: str, expected: MediaCapability, now: int | None = None
    ) -> MediaCapability:
        """Verify signature, expiry and the complete expected authorization scope."""
        version, payload, supplied_signature = _split_token(token)
        capability = _decode_capability(payload)
        if version != capability.signer_version or version != expected.signer_version:
            raise MediaCapabilityError("unsupported media capability version")

        expected_signature = _encode(
            hmac.new(self._signing_key, payload.encode("ascii"), hashlib.sha256).digest()
        )
        signature_is_valid = hmac.compare_digest(supplied_signature, expected_signature)
        scope_is_expected = (
            capability.resource_kind == expected.resource_kind
            and capability.resource_id == expected.resource_id
            and capability.method == expected.method.upper()
            and (
                expected.session_parent_id is None
                or capability.session_parent_id == expected.session_parent_id
            )
        )
        if not signature_is_valid or not scope_is_expected:
            raise MediaCapabilityError("invalid media capability")

        current_time = int(time.time()) if now is None else now
        if capability.expires_at < current_time:
            raise MediaCapabilityError("expired media capability")
        return capability

    def signed_url(self, path: str, capability: MediaCapability) -> str:
        """Attach this capability to an API-relative media path."""
        return f"{path}?{urlencode({'token': self.issue(capability)})}"


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _split_token(token: str) -> tuple[str, str, str]:
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise MediaCapabilityError("malformed media capability")
    return parts[0], parts[1], parts[2]


def _decode_capability(payload: str) -> MediaCapability:
    try:
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(f"{payload}{padding}").decode("ascii")
        version, method, kind, resource_id, expires_at, parent = decoded.split(":")
        resource_kind = _parse_resource_kind(kind)
        return MediaCapability(
            resource_kind=resource_kind,
            resource_id=int(resource_id),
            method=method,
            expires_at=int(expires_at),
            session_parent_id=int(parent) if parent else None,
            signer_version=version,
        )
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise MediaCapabilityError("malformed media capability") from error


def _parse_resource_kind(value: str) -> MediaResourceKind:
    match value:
        case "file" | "session_stream" | "session_hls" | "image":
            return value
        case _:
            raise MediaCapabilityError("invalid media resource kind")
