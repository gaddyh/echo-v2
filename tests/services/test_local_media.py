"""Tests for the confined local-media (file://) reader."""

from __future__ import annotations

from pathlib import Path

import pytest

from echo_v2.services.local_media import (
    LocalMediaError,
    local_media_root,
    read_local_media_url,
)


def test_local_media_root_honors_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAHA_MEDIA_CACHE_DIR", str(tmp_path))
    assert local_media_root() == tmp_path.resolve()


def test_reads_file_inside_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAHA_MEDIA_CACHE_DIR", str(tmp_path))
    media = tmp_path / "audio-abc.ogg"
    media.write_bytes(b"payload")
    assert read_local_media_url(media.as_uri()) == b"payload"


def test_rejects_path_outside_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setenv("WAHA_MEDIA_CACHE_DIR", str(root))
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")
    with pytest.raises(LocalMediaError, match="escapes"):
        read_local_media_url(outside.as_uri())


def test_rejects_existing_file_outside_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setenv("WAHA_MEDIA_CACHE_DIR", str(root))
    passwd = Path("/etc/passwd")
    if not passwd.exists():
        pytest.skip("/etc/passwd not present")
    with pytest.raises(LocalMediaError, match="escapes"):
        read_local_media_url(passwd.as_uri())


def test_rejects_symlink_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setenv("WAHA_MEDIA_CACHE_DIR", str(root))
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"x")
    (root / "link.bin").symlink_to(secret)
    with pytest.raises(LocalMediaError, match="escapes"):
        read_local_media_url((root / "link.bin").as_uri())


def test_rejects_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setenv("WAHA_MEDIA_CACHE_DIR", str(root))
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"x")
    with pytest.raises(LocalMediaError, match="escapes"):
        read_local_media_url(f"file://{root}/../secret.bin")


def test_rejects_remote_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAHA_MEDIA_CACHE_DIR", str(tmp_path))
    with pytest.raises(LocalMediaError, match="host"):
        read_local_media_url("file://evil.example/share/file.mp4")


def test_rejects_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAHA_MEDIA_CACHE_DIR", str(tmp_path))
    with pytest.raises(LocalMediaError, match="not found"):
        read_local_media_url((tmp_path / "gone.bin").as_uri())


def test_explicit_allowed_root(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    media = root / "v.mp4"
    media.write_bytes(b"v")
    assert read_local_media_url(media.as_uri(), allowed_root=root) == b"v"
    other = tmp_path / "other.mp4"
    other.write_bytes(b"x")
    with pytest.raises(LocalMediaError, match="escapes"):
        read_local_media_url(other.as_uri(), allowed_root=root)
