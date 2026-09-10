"""Webhook deduplication store.

Providers deliver notifications at-least-once. When a provider does not
receive a timely HTTP 200, it resends the same notification (Green retries
after roughly a minute). The webhook route uses a :class:`WebhookDedupStore`
to claim each notification exactly once within a process; a future persistent
implementation (Redis/Postgres) will extend this across workers and restarts.

The store deduplicates on ``event_id`` -- the provider-assigned identity of a
*notification*, not of a message. See :class:`ProviderMessageEvent` and
siblings for the ``event_id`` format per event type.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = [
    "InMemoryWebhookDedupStore",
    "WebhookDedupStore",
]


@runtime_checkable
class WebhookDedupStore(Protocol):
    """Claim a notification for processing.

    Returns ``True`` only for the first claim of ``key`` within the store's
    retention window. Subsequent claims for the same key return ``False``,
    indicating a duplicate that should not be reprocessed.

    The optional metadata kwargs (``provider``, ``connection_id``,
    ``event_type``) are stored by the Postgres implementation for
    operational visibility. The in-memory implementation ignores them.
    """

    async def claim(
        self,
        key: str,
        *,
        provider: str | None = None,
        connection_id: str | None = None,
        event_type: str | None = None,
    ) -> bool:
        """Atomically claim ``key``. ``True`` if this is the first claim."""
        ...


class InMemoryWebhookDedupStore:
    """Process-local dedup store backed by a set.

    Suitable for Step 0 and tests only. It does not survive process restarts,
    does not coordinate across workers, and grows without bound. A persistent
    implementation (Redis ``SETNX`` with TTL, or a Postgres insert-on-conflict)
    is required before real-user onboarding.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    async def claim(
        self,
        key: str,
        *,
        provider: str | None = None,
        connection_id: str | None = None,
        event_type: str | None = None,
    ) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


# Structural check: InMemoryWebhookDedupStore satisfies the protocol.
_: WebhookDedupStore = InMemoryWebhookDedupStore()  # type: ignore[assignment]
