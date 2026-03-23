from pathlib import Path

from _pytest.monkeypatch import MonkeyPatch

from src.core.config import settings
from src.services.video_source_validator import validate_video_source_path


def _create_sample_video(root: Path) -> None:
    folder = root / "2026031310"
    folder.mkdir(parents=True, exist_ok=True)
    file_path = folder / "01M02S_1772091092.mp4"
    file_path.write_bytes(b"test")


def test_validate_video_source_path_success(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "VIDEO_ROOT_PATH", str(tmp_path))
    _create_sample_video(tmp_path)

    result = validate_video_source_path(str(tmp_path))
    assert result.valid is True
    assert result.file_count == 1
    assert result.latest_file_time is not None
    assert result.earliest_file_time is not None


def test_validate_video_source_path_rejects_outside_root(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "VIDEO_ROOT_PATH", str(tmp_path / "allowed"))
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)

    result = validate_video_source_path(str(outside))
    assert result.valid is False
    assert "outside" in result.message
