"""Tests for resolve_allowed_path path confinement (todo 19)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.security import PathSecurityError, resolve_allowed_path


@pytest.fixture()
def layout(tmp_path: Path) -> dict[str, Path]:
    """Allowed root with a real file, plus an outside file to escape to."""
    root = tmp_path / "allowed"
    (root / "sub").mkdir(parents=True)
    inside = root / "sub" / "clip.mp4"
    inside.write_bytes(b"\x00" * 16)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "secret.mp4"
    outside.write_bytes(b"\x00" * 16)
    return {"root": root, "inside": inside, "outside": outside}


def test_absolute_path_inside_root_returns_resolved(layout: dict[str, Path]) -> None:
    resolved = resolve_allowed_path(str(layout["inside"]), [layout["root"]])
    assert resolved == layout["inside"].resolve()
    assert resolved.is_file()


def test_absolute_path_outside_root_raises(layout: dict[str, Path]) -> None:
    with pytest.raises(PathSecurityError, match="outside the allowed roots"):
        resolve_allowed_path(str(layout["outside"]), [layout["root"]])


def test_parent_of_root_is_not_inside_root(layout: dict[str, Path]) -> None:
    # The root's parent contains the root, not the other way around.
    with pytest.raises(PathSecurityError):
        resolve_allowed_path(str(layout["root"].parent), [layout["root"]])


def test_dotdot_traversal_raises(layout: dict[str, Path]) -> None:
    # Normalises back to a root-relative location, but '..' is rejected.
    back_inside = str(layout["root"] / "sub" / ".." / "sub" / "clip.mp4")
    with pytest.raises(PathSecurityError, match="normalised"):
        resolve_allowed_path(back_inside, [layout["root"]])
    with pytest.raises(PathSecurityError, match="normalised"):
        resolve_allowed_path(f"{layout['root']}/../../../etc/passwd", [layout["root"]])


def test_relative_path_raises(layout: dict[str, Path]) -> None:
    with pytest.raises(PathSecurityError, match="absolute"):
        resolve_allowed_path("sub/clip.mp4", [layout["root"]])
    with pytest.raises(PathSecurityError, match="absolute"):
        resolve_allowed_path("../../etc/passwd", [layout["root"]])


def test_symlink_escaping_root_raises(layout: dict[str, Path]) -> None:
    link = layout["root"] / "sneaky.mp4"
    os.symlink(layout["outside"], link)
    with pytest.raises(PathSecurityError, match="outside the allowed roots"):
        resolve_allowed_path(str(link), [layout["root"]])


def test_symlink_staying_inside_root_is_allowed(layout: dict[str, Path]) -> None:
    link = layout["root"] / "alias.mp4"
    os.symlink(layout["inside"], link)
    resolved = resolve_allowed_path(str(link), [layout["root"]])
    assert resolved == layout["inside"].resolve()


def test_missing_file_inside_root_raises(layout: dict[str, Path]) -> None:
    with pytest.raises(PathSecurityError, match="not a regular file"):
        resolve_allowed_path(str(layout["root"] / "sub" / "nope.mp4"), [layout["root"]])


def test_root_itself_is_rejected(layout: dict[str, Path]) -> None:
    # The root is a directory, not a regular file.
    with pytest.raises(PathSecurityError, match="not a regular file"):
        resolve_allowed_path(str(layout["root"]), [layout["root"]])


def test_empty_roots_allow_nothing(layout: dict[str, Path]) -> None:
    with pytest.raises(PathSecurityError, match="outside the allowed roots"):
        resolve_allowed_path(str(layout["inside"]), [])


def test_empty_string_raises(layout: dict[str, Path]) -> None:
    with pytest.raises(PathSecurityError, match="non-empty"):
        resolve_allowed_path("", [layout["root"]])


def test_symlinked_root_still_confining(tmp_path: Path) -> None:
    # Root given as a symlink: allowed_roots() resolves it, and a path via
    # the symlinked root must resolve to the real location inside it.
    real = tmp_path / "real"
    real.mkdir()
    target = real / "clip.mp4"
    target.write_bytes(b"\x00")
    link_root = tmp_path / "rootlink"
    os.symlink(real, link_root)
    resolved = resolve_allowed_path(str(target), [link_root])
    assert resolved == target
