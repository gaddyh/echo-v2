"""DailyDigestRepository — manages daily_digests rows.

Protocol + in-memory + Postgres implementations. The key operation is
``claim_or_get``: atomically insert a ``processing`` row for
``(user_id, local_date)`` or return the existing row. This provides
dedup — if the worker crashes after sending but before updating
status, the row already exists and won't be re-claimed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Protocol, runtime_checkable

from echo_v2.domain.digest import DailyDigest, DailyDigestStatus

__all__ = [
    "DailyDigestRepository",
    "InMemoryDailyDigestRepository",
]


@runtime_checkable
class DailyDigestRepository(Protocol):
    """Manage daily_digests rows with atomic claim semantics."""

    async def claim_or_get(
        self,
        *,
        user_id: str,
        local_date: date,
    ) -> DailyDigest | None:
        """Atomically insert a ``processing`` row or return the existing one.

        Returns ``None`` if a row already exists for this ``(user_id,
        local_date)`` — meaning the digest was already claimed (or
        completed) today. Returns the new ``DailyDigest`` if the insert
        succeeded (this is a fresh claim).
        """
        ...

    async def update_status(
        self,
        *,
        digest_id: str,
        status: DailyDigestStatus,
        sent_at: datetime | None = None,
        provider_message_id: str | None = None,
        item_count: int = 0,
    ) -> bool:
        """Update the status of a digest. Returns ``True`` if updated."""
        ...

    async def get(
        self,
        *,
        user_id: str,
        local_date: date,
    ) -> DailyDigest | None:
        """Get the digest for a user/date, or ``None``."""
        ...


class InMemoryDailyDigestRepository:
    """Process-local daily digest repository backed by a dict."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, date], DailyDigest] = {}
        self._counter = 0

    async def claim_or_get(
        self,
        *,
        user_id: str,
        local_date: date,
    ) -> DailyDigest | None:
        key = (user_id, local_date)
        if key in self._rows:
            return None  # already claimed
        self._counter += 1
        digest = DailyDigest(
            id=f"digest-{self._counter}",
            user_id=user_id,
            local_date=local_date,
            status=DailyDigestStatus.PROCESSING,
            created_at=datetime.now(timezone.utc),
        )
        self._rows[key] = digest
        return digest

    async def update_status(
        self,
        *,
        digest_id: str,
        status: DailyDigestStatus,
        sent_at: datetime | None = None,
        provider_message_id: str | None = None,
        item_count: int = 0,
    ) -> bool:
        for key, digest in self._rows.items():
            if digest.id == digest_id:
                self._rows[key] = DailyDigest(
                    id=digest.id,
                    user_id=digest.user_id,
                    local_date=digest.local_date,
                    status=status,
                    created_at=digest.created_at,
                    sent_at=sent_at,
                    provider_message_id=provider_message_id,
                    item_count=item_count,
                )
                return True
        return False

    async def get(
        self,
        *,
        user_id: str,
        local_date: date,
    ) -> DailyDigest | None:
        return self._rows.get((user_id, local_date))


_dig_repo: DailyDigestRepository = InMemoryDailyDigestRepository()  # type: ignore[assignment]
