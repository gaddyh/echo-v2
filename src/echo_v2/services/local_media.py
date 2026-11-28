"""Trusted local-media handoff shared by all media consumers.

Media resolvers hand decrypted plaintext to the enrichment pipeline as
``file://`` paths into a dedicated cache directory
(:func:`local_media_root`). Every consumer that accepts ``file://`` must
route through :func:`read_local_media_url`: it confines reads to the
resolver root and follows symlinks before deciding, so neither relative
traversal nor a symlink planted inside the cache can read arbitrary files.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

__all__ = ["LocalMediaError", "local_media_root", "read_local_media_url"]


class LocalMediaError(RuntimeError):
    """A ``file://`` media path is malformed, missing, or outside the root."""


def local_media_root() -> Path:
    """The only directory media resolvers may hand ``file://`` paths out of.

    Producers (``WahaMediaUrlResolver``) and consumers resolve the same root
    so reads and writes can never drift apart.
    """
    root = os.environ.get("WAHA_MEDIA_CACHE_DIR")
    base = Path(root) if root else Path(tempfile.gettempdir()) / "echo-waha-media"
    return base.resolve()


def read_local_media_url(url: str, *, allowed_root: Path | None = None) -> bytes:
    """Read a resolver-issued ``file://`` path, confined to the cache root."""
    parsed = urlparse(url)
    if parsed.netloc not in ("", "localhost"):
        raise LocalMediaError("local media path must not reference a host")
    resolved = Path(unquote(parsed.path)).resolve()
    root = (allowed_root or local_media_root()).resolve()
    if not resolved.is_relative_to(root):
        raise LocalMediaError("local media path escapes the resolver cache")
    if not resolved.is_file():
        raise LocalMediaError(f"local media file not found: {resolved.name}")
    return resolved.read_bytes()
