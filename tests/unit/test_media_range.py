from io import BytesIO

from src.api.v1.endpoints.media import (
    _clamp_range_end,
    send_bytes_range_requests,
)


def _make_file(payload: bytes) -> BytesIO:
    return BytesIO(payload)


def test_clamp_range_end_keeps_valid_end_unchanged() -> None:
    # file_size=10, valid last offset = 9
    assert _clamp_range_end(9, 10) == 9
    assert _clamp_range_end(0, 10) == 0


def test_clamp_range_end_caps_at_last_valid_offset() -> None:
    # clients sometimes request up to file_size (one past the last offset)
    assert _clamp_range_end(10, 10) == 9
    assert _clamp_range_end(100, 10) == 9


def test_send_bytes_range_requests_returns_full_range_when_end_within_file() -> None:
    payload = b"abcdefghij"  # 10 bytes
    chunks = list(send_bytes_range_requests(_make_file(payload), start=0, end=9))
    assert b"".join(chunks) == payload
    assert all(chunk for chunk in chunks)


def test_send_bytes_range_requests_terminates_when_end_equals_file_size() -> None:
    # Regression: previously the generator yielded zero-byte pieces in an
    # infinite loop when ``end == file_size`` because ``f.read`` could not
    # advance past EOF while ``pos <= end`` stayed true.
    payload = b"abcdefghij"  # 10 bytes
    chunks = list(send_bytes_range_requests(_make_file(payload), start=0, end=10))
    assert b"".join(chunks) == payload
    assert all(chunk for chunk in chunks)


def test_send_bytes_range_requests_handles_open_ended_high_end() -> None:
    # Defensive: an absurd end offset must not hang the generator.
    payload = b"abcdefghij"  # 10 bytes
    chunks = list(send_bytes_range_requests(_make_file(payload), start=0, end=10**9))
    assert b"".join(chunks) == payload


def test_send_bytes_range_requests_respects_partial_range() -> None:
    payload = b"abcdefghij"
    chunks = list(send_bytes_range_requests(_make_file(payload), start=3, end=6))
    assert b"".join(chunks) == b"defg"


def test_send_bytes_range_requests_yields_multiple_chunks() -> None:
    payload = b"x" * (3 * 1024 * 1024 + 17)
    chunks = list(send_bytes_range_requests(_make_file(payload), start=0, end=len(payload) - 1))
    assert len(chunks) > 1
    assert b"".join(chunks) == payload
