"""Persistence for versioned message enrichment state."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Any, Protocol, cast, runtime_checkable

from sqlalchemy import CursorResult, and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.media_enrichment import (
    EnrichmentKind,
    EnrichmentStatus,
    MessageEnrichment,
)
from echo_v2.persistence.orm import MessageEnrichmentRow


@runtime_checkable
class MessageEnrichmentRepository(Protocol):  # pragma: no cover - interface only
    async def get(self, *, message_id: str, enrichment_version: str) -> MessageEnrichment | None: ...

    async def claim(
        self,
        *,
        message_id: str,
        enrichment_version: str,
        owner: str,
        lease_seconds: int,
        now: datetime,
    ) -> MessageEnrichment | None: ...

    async def complete(
        self,
        *,
        message_id: str,
        enrichment_version: str,
        owner: str,
        kind: EnrichmentKind,
        text: str,
        model: str | None,
        now: datetime,
    ) -> bool: ...

    async def fail(
        self,
        *,
        message_id: str,
        enrichment_version: str,
        owner: str,
        error: str,
        next_retry_at: datetime | None,
        terminal: bool,
        now: datetime,
    ) -> bool: ...


class InMemoryMessageEnrichmentRepository:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], MessageEnrichment] = {}
        self._lock = asyncio.Lock()

    async def get(self, *, message_id: str, enrichment_version: str) -> MessageEnrichment | None:
        async with self._lock:
            return self.records.get((message_id, enrichment_version))

    async def claim(self, *, message_id: str, enrichment_version: str, owner: str,
                    lease_seconds: int, now: datetime) -> MessageEnrichment | None:
        async with self._lock:
            key = (message_id, enrichment_version)
            current = self.records.get(key)
            if current is not None:
                if current.status is EnrichmentStatus.COMPLETED or current.status is EnrichmentStatus.TERMINAL:
                    return None
                if current.next_retry_at is not None and current.next_retry_at > now:
                    return None
                if (current.status is EnrichmentStatus.PROCESSING
                        and current.lease_expires_at is not None
                        and current.lease_expires_at > now):
                    return None
            attempts = (current.attempts if current else 0) + 1
            claimed = MessageEnrichment(
                message_id=message_id,
                enrichment_version=enrichment_version,
                status=EnrichmentStatus.PROCESSING,
                kind=current.kind if current else None,
                text=current.text if current else None,
                model=current.model if current else None,
                error=None,
                attempts=attempts,
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                next_retry_at=None,
                enriched_at=current.enriched_at if current else None,
                updated_at=now,
            )
            self.records[key] = claimed
            return claimed

    async def complete(self, *, message_id: str, enrichment_version: str, owner: str,
                       kind: EnrichmentKind, text: str, model: str | None, now: datetime) -> bool:
        async with self._lock:
            key = (message_id, enrichment_version)
            current = self.records.get(key)
            if current is None or current.lease_owner != owner or current.status is not EnrichmentStatus.PROCESSING:
                return False
            self.records[key] = MessageEnrichment(
                message_id=message_id, enrichment_version=enrichment_version,
                status=EnrichmentStatus.COMPLETED, kind=kind, text=text, model=model,
                attempts=current.attempts, enriched_at=now, updated_at=now,
            )
            return True

    async def fail(self, *, message_id: str, enrichment_version: str, owner: str,
                   error: str, next_retry_at: datetime | None, terminal: bool, now: datetime) -> bool:
        async with self._lock:
            key = (message_id, enrichment_version)
            current = self.records.get(key)
            if current is None or current.lease_owner != owner:
                return False
            self.records[key] = MessageEnrichment(
                message_id=message_id, enrichment_version=enrichment_version,
                status=EnrichmentStatus.TERMINAL if terminal else EnrichmentStatus.FAILED,
                kind=current.kind, text=None, model=current.model, error=error,
                attempts=current.attempts, next_retry_at=next_retry_at, updated_at=now,
            )
            return True


class PostgresMessageEnrichmentRepository:  # pragma: no cover - exercised by Postgres integration tests
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, *, message_id: str, enrichment_version: str) -> MessageEnrichment | None:
        async with self._session_factory() as session:
            row = (await session.execute(select(MessageEnrichmentRow).where(
                MessageEnrichmentRow.message_id == message_id,
                MessageEnrichmentRow.enrichment_version == enrichment_version,
            ))).scalar_one_or_none()
            return _to_domain(row) if row else None

    async def claim(self, *, message_id: str, enrichment_version: str, owner: str,
                    lease_seconds: int, now: datetime) -> MessageEnrichment | None:
        expires = now + timedelta(seconds=lease_seconds)
        async with self._session_factory() as session:
            insert = pg_insert(MessageEnrichmentRow).values(
                id=str(uuid.uuid4()), message_id=message_id,
                enrichment_version=enrichment_version, status=EnrichmentStatus.PROCESSING.value,
                lease_owner=None, lease_expires_at=None, attempts=0,
                updated_at=now,
            ).on_conflict_do_nothing(index_elements=["message_id", "enrichment_version"])
            await session.execute(insert)
            stmt = update(MessageEnrichmentRow).where(
                MessageEnrichmentRow.message_id == message_id,
                MessageEnrichmentRow.enrichment_version == enrichment_version,
                or_(
                    MessageEnrichmentRow.status.in_([
                        EnrichmentStatus.PENDING.value,
                        EnrichmentStatus.FAILED.value,
                    ]),
                    and_(
                        MessageEnrichmentRow.status == EnrichmentStatus.PROCESSING.value,
                        MessageEnrichmentRow.lease_expires_at <= now,
                    ),
                ),
                or_(MessageEnrichmentRow.next_retry_at.is_(None), MessageEnrichmentRow.next_retry_at <= now),
            ).values(
                status=EnrichmentStatus.PROCESSING.value,
                lease_owner=owner,
                lease_expires_at=expires,
                attempts=MessageEnrichmentRow.attempts + 1,
                error=None,
                next_retry_at=None,
                updated_at=now,
            )
            await session.execute(stmt)
            await session.commit()
        return await self.get(message_id=message_id, enrichment_version=enrichment_version)

    async def complete(self, *, message_id: str, enrichment_version: str, owner: str,
                       kind: EnrichmentKind, text: str, model: str | None, now: datetime) -> bool:
        async with self._session_factory() as session:
            result = cast(CursorResult[Any], await session.execute(update(MessageEnrichmentRow).where(
                MessageEnrichmentRow.message_id == message_id,
                MessageEnrichmentRow.enrichment_version == enrichment_version,
                MessageEnrichmentRow.status == EnrichmentStatus.PROCESSING.value,
                MessageEnrichmentRow.lease_owner == owner,
            ).values(status=EnrichmentStatus.COMPLETED.value, kind=kind.value, text=text,
                     model=model, enriched_at=now, lease_owner=None, lease_expires_at=None,
                     updated_at=now)))
            await session.commit()
            return result.rowcount == 1

    async def fail(self, *, message_id: str, enrichment_version: str, owner: str,
                   error: str, next_retry_at: datetime | None, terminal: bool, now: datetime) -> bool:
        async with self._session_factory() as session:
            result = cast(CursorResult[Any], await session.execute(update(MessageEnrichmentRow).where(
                MessageEnrichmentRow.message_id == message_id,
                MessageEnrichmentRow.enrichment_version == enrichment_version,
                MessageEnrichmentRow.status == EnrichmentStatus.PROCESSING.value,
                MessageEnrichmentRow.lease_owner == owner,
            ).values(status=(EnrichmentStatus.TERMINAL if terminal else EnrichmentStatus.FAILED).value,
                     error=error, next_retry_at=next_retry_at, lease_owner=None,
                     lease_expires_at=None, updated_at=now)))
            await session.commit()
            return result.rowcount == 1


def _to_domain(row: MessageEnrichmentRow) -> MessageEnrichment:  # pragma: no cover - Postgres adapter path
    return MessageEnrichment(
        message_id=str(row.message_id), enrichment_version=row.enrichment_version,
        status=EnrichmentStatus(row.status),
        kind=EnrichmentKind(row.kind) if row.kind else None, text=row.text, model=row.model,
        error=row.error, attempts=row.attempts, lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at, next_retry_at=row.next_retry_at,
        enriched_at=row.enriched_at, updated_at=row.updated_at,
    )


__all__ = ["InMemoryMessageEnrichmentRepository", "MessageEnrichmentRepository", "PostgresMessageEnrichmentRepository"]
