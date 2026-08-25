from src.core.security import create_access_token, get_password_hash, verify_password
from src.services.media_signing import MediaCapability, MediaCapabilityError, MediaSigningService


def test_password_hashing():
    password = "test_password123"
    hashed = get_password_hash(password)
    assert hashed != password
    assert verify_password(password, hashed) is True
    assert verify_password("wrong_password", hashed) is False


def test_create_access_token():
    user_id = 1
    token = create_access_token(subject=str(user_id))
    assert isinstance(token, str)
    assert len(token) > 0


def test_media_capability_validates_complete_resource_scope() -> None:
    service = MediaSigningService("test-media-signing-key")
    capability = MediaCapability(
        resource_kind="file",
        resource_id=7,
        method="GET",
        expires_at=2_000,
        session_parent_id=3,
    )

    token = service.issue(capability)

    service.verify(token, capability, now=1_999)
    assert "/" not in token
    assert "tmp" not in token


def test_media_capability_rejects_expired_tampered_and_cross_scope_tokens() -> None:
    service = MediaSigningService("test-media-signing-key")
    capability = MediaCapability(
        resource_kind="file",
        resource_id=7,
        method="GET",
        expires_at=2_000,
        session_parent_id=3,
    )
    token = service.issue(capability)

    for expected, now in (
        (capability, 2_001),
        (MediaCapability("file", 8, "GET", 2_000, 3), 1_999),
        (MediaCapability("file", 7, "GET", 2_000, 4), 1_999),
        (MediaCapability("file", 7, "POST", 2_000, 3), 1_999),
    ):
        try:
            service.verify(token, expected, now=now)
        except MediaCapabilityError:
            continue
        raise AssertionError("Expected the media capability verification to fail")

    tampered = f"{token[:-1]}x"
    try:
        service.verify(tampered, capability, now=1_999)
    except MediaCapabilityError:
        return
    raise AssertionError("Expected a tampered media capability to fail")
