"""SQLAlchemy ORM models for the Echo v2 Foundation DB.

Four tables — ``users``, ``whatsapp_connections``,
``provider_webhook_events``, ``idempotency_operations`` — backing the three
existing repository protocols plus the user anchor.

Style rules (see plan):
* Plain ``Mapped[...]`` classes. No lazy-loading relationships.
* Foreign keys declared but not eagerly joined.
* Finite states use ``TEXT`` + ``CHECK`` (not PostgreSQL ``ENUM``) so
  evolving the value set later is a migration, not ``ALTER TYPE``.
* ``created_at`` set once by DB default ``now()``; ``updated_at`` set
  explicitly by every repository ``UPDATE`` (no trigger magic).
* The migration DDL is the source of truth for production schema; these
  models exist for repository mapping and test convenience.

ORM objects never escape the persistence layer — repositories translate
between domain dataclasses and these rows.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
)
from sqlalchemy.types import (
    TIMESTAMP,
    LargeBinary,
    Uuid,
)

__all__ = [
    "Base",
    "ContactRow",
    "IdempotencyOperationRow",
    "ProviderWebhookEventRow",
    "ScheduledActionRow",
    "UserRow",
    "WhatsAppConnectionRow",
]


class Base(DeclarativeBase):
    """Declarative base for all Echo ORM models."""


# --- users -----------------------------------------------------------------


class UserRow(Base):
    """Echo user / account owner — the anchor for everything else."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    phone_number: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    timezone: Mapped[str | None] = mapped_column(Text, nullable=True)
    locale: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="active",
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "account_status IN ('active', 'suspended', 'deleted')",
            name="users_account_status_check",
        ),
    )


# --- whatsapp_connections --------------------------------------------------


class WhatsAppConnectionRow(Base):
    """A user's connection to their WhatsApp via a provider (Green today)."""

    __tablename__ = "whatsapp_connections"

    id: Mapped[str] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[str] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_connection_id: Mapped[str] = mapped_column(Text, nullable=False)
    credentials: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    webhook_token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    connection_status: Mapped[str] = mapped_column(Text, nullable=False)
    provider_raw_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="whatsapp_connections_user_provider_key"),
        UniqueConstraint(
            "provider",
            "provider_connection_id",
            name="whatsapp_connections_provider_id_key",
        ),
        CheckConstraint(
            "connection_status IN ("
            "'provisioning','connecting','pairing_required','connected',"
            "'degraded','blocked','suspended','unknown'"
            ")",
            name="whatsapp_connections_status_check",
        ),
    )


# --- provider_webhook_events -----------------------------------------------


class ProviderWebhookEventRow(Base):
    """Receipt/dedupe metadata for a provider webhook notification.

    No raw provider payloads — provider JSON never escapes the integration
    boundary. ``connection_id`` is NOT NULL: an unknown instance is rejected
    at auth (no matching ``webhook_token_hash``) before reaching the dedup
    store, so every authenticated dedupe record has a connection.
    """

    __tablename__ = "provider_webhook_events"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    connection_id: Mapped[str] = mapped_column(
        Uuid,
        ForeignKey("whatsapp_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


# --- idempotency_operations ------------------------------------------------


class IdempotencyOperationRow(Base):
    """Persistent idempotency record with lease + fencing-token semantics.

    See the plan's "Idempotency concurrency" section: ``reserve()`` atomically
    claims or reclaims an expired lease; every owner write is token-guarded
    (``WHERE owner_token = :my_token AND state = 'IN_PROGRESS'``) so a slow
    prior owner cannot overwrite a new owner's outcome.
    """

    __tablename__ = "idempotency_operations"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    owner_token: Mapped[str | None] = mapped_column(Uuid, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    outcome: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    attempts: Mapped[int | None] = mapped_column(nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(nullable=True)
    error_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('IN_PROGRESS', 'SUCCESS', 'FAILURE', 'INDETERMINATE')",
            name="idempotency_operations_state_check",
        ),
        Index(
            "ix_idempotency_operations_state_lease",
            "state",
            "lease_expires_at",
        ),
    )


# --- scheduled_actions ----------------------------------------------------


class ScheduledActionRow(Base):
    """A persisted future action (roadmap §8.4).

    The scheduler claims due rows via ``FOR UPDATE SKIP LOCKED`` on the
    ``(status, execute_at_utc)`` index, sets ``status='in_progress'`` and
    ``claimed_at=now()``, then executes. Stale ``in_progress`` rows
    (``claimed_at`` older than the lease) are reset to ``pending`` on
    restart via :meth:`ScheduledActionRepository.recover_stale`.
    """

    __tablename__ = "scheduled_actions"

    id: Mapped[str] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    execute_at_utc: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
    )
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','in_progress','succeeded','failed',"
            "'cancelled','indeterminate')",
            name="scheduled_actions_status_check",
        ),
        CheckConstraint(
            "type IN ('send_whatsapp_message','send_reminder')",
            name="scheduled_actions_type_check",
        ),
        Index(
            "ix_scheduled_actions_status_execute_at",
            "status",
            "execute_at_utc",
        ),
    )


# --- contacts --------------------------------------------------------------


class ContactRow(Base):
    """A saved contact for a user — populated from vCards sent to the bot.

    Unique on ``(user_id, phone_number)``: sending the same contact again
    updates the display name. Lookup by ``display_name`` lets the user type
    a name instead of re-sending the vCard.
    """

    __tablename__ = "contacts"

    id: Mapped[str] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[str] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    phone_number: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "phone_number",
            name="contacts_user_phone_key",
        ),
        Index(
            "ix_contacts_user_id",
            "user_id",
        ),
    )
