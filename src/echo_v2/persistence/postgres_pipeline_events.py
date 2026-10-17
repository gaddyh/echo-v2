"""PostgreSQL repository for structured pipeline events."""

from __future__ import annotations

import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.pipeline_event import PipelineEvent
from echo_v2.persistence.orm import PipelineEventRow


class PostgresPipelineEventRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, event: PipelineEvent) -> None:
        async with self._session_factory() as session:
            await session.execute(
                pg_insert(PipelineEventRow).values(
                    id=str(uuid.uuid4()),
                    event_name=event.event_name,
                    pipeline=event.pipeline,
                    status=event.status,
                    message_id=event.message_id,
                    event_id=event.event_id,
                    user_id_hash=event.user_id_hash,
                    chat_id_hash=event.chat_id_hash,
                    attempt=event.attempt,
                    duration_ms=event.duration_ms,
                    error_type=event.error_type,
                    error_message=event.error_message,
                    event_metadata=event.metadata,
                    created_at=event.created_at,
                )
            )
            await session.commit()


__all__ = ["PostgresPipelineEventRepository"]
