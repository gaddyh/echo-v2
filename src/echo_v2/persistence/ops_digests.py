"""Persistence port for connector ops digests.

The internal ops-alert endpoint records one row per hourly digest; the
admin health dashboard lists them. ``record`` is idempotent on
``digest_key`` so connector delivery retries never duplicate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from echo_v2.domain.ops_digest import OpsDigest


@runtime_checkable
class OpsDigestRepository(Protocol):
    async def record(self, digest: OpsDigest) -> None:
        """Persist one digest. Idempotent on ``digest_key``."""
        ...

    async def list_since(
        self,
        *,
        since: datetime,
        limit: int = 100,
    ) -> list[OpsDigest]:
        """Return digests received at or after ``since``, newest first."""
        ...

    async def latest(self) -> OpsDigest | None:
        """Return the most recently received digest, if any."""
        ...


__all__ = ["OpsDigestRepository"]
