"""GreenInstancePool — pre-created Green API instances for fast onboarding.

The pool maintains up to ``target_size`` pre-created Green instances that
have already reached ``notAuthorized`` (ready for pairing). Onboarding
claims an available instance instantly instead of waiting for
``createInstance`` + the ready poll on the user's critical path.

Lifecycle of a pool row (see ``green_instance_pool`` table):

* ``creating`` — instance is being created in Green. Credentials are
  persisted immediately after ``createInstance`` (via ``mark_created``),
  *before* the ready poll, so a crash during waiting leaves a recoverable
  row with the Green instance id.
* ``available`` — Green confirmed ``notAuthorized``. Ready to be claimed.
* ``claimed`` — atomically reserved by a user. ``finalize_claim`` deletes
  the row after the connection has been persisted to
  ``whatsapp_connections``. ``release_claim`` returns it to ``available``
  during recovery (crash before connection save, Green still notAuthorized).
* ``failed`` — terminal. Cleaned on next ``ensure_capacity``, with a
  best-effort Green ``deleteInstance`` if it carries a
  ``provider_connection_id`` (so pool failures don't eat Green quota).

Capacity reservation: ``creating`` + ``available`` count toward the
target size; ``claimed`` does not (it's out of the reserve pool). This
means refill can start immediately after a claim.

The pool is optional: ``OnboardingService`` accepts ``pool=None`` and
falls back to synchronous on-demand creation (the current behavior).
``GREEN_INSTANCE_POOL_SIZE=0`` disables the pool entirely.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import TYPE_CHECKING

from echo_v2.persistence.green_instance_pool import (
    GreenInstancePoolRepository,
    PooledInstance,
)
from echo_v2.ports.whatsapp import (
    ConnectionConfig,
    ConnectionRef,
    WhatsAppEventSubscription,
)

if TYPE_CHECKING:
    from echo_v2.integrations.green.client import GreenClient
    from echo_v2.integrations.green.provisioner import GreenProvisioner
    from echo_v2.persistence.whatsapp_connections import (
        WhatsAppConnectionRepository,
    )

__all__ = ["GreenInstancePool"]

_logger = logging.getLogger("echo_v2.services.green_instance_pool")


class GreenInstancePool:
    """Manages a pool of pre-created, ready-to-pair Green API instances.

    Dependencies:
    * ``provisioner`` — creates Green instances (``create_connection``) and
      deletes them (``delete_connection``) for cleanup.
    * ``green_client`` — polls ``getStateInstance`` during warm-up.
    * ``repo`` — persists pool rows (``green_instance_pool`` table).
    * ``connection_repo`` — used in recovery to check whether a claimed row
      already has a matching ``whatsapp_connections`` row.
    * ``webhook_base_url`` — the public URL Green should send webhooks to.
    * ``target_size`` — how many ``creating`` + ``available`` rows to maintain.
    """

    def __init__(
        self,
        *,
        provisioner: GreenProvisioner,
        green_client: GreenClient,
        repo: GreenInstancePoolRepository,
        connection_repo: WhatsAppConnectionRepository,
        webhook_base_url: str,
        target_size: int = 1,
        ready_poll_interval: float = 2.0,
        ready_max_attempts: int = 90,
        create_backoff_seconds: float = 30.0,
    ) -> None:
        self._provisioner = provisioner
        self._green_client = green_client
        self._repo = repo
        self._connection_repo = connection_repo
        self._webhook_base_url = webhook_base_url.rstrip("/")
        self._target_size = target_size
        self._ready_poll_interval = ready_poll_interval
        self._ready_max_attempts = ready_max_attempts
        self._create_backoff_seconds = create_backoff_seconds
        self._refill_task: asyncio.Task[None] | None = None

    # --- public API -------------------------------------------------------

    async def ensure_capacity(self) -> None:
        """Recover stale rows + create up to ``target_size`` instances.

        Called on startup (as a background task). Idempotent: safe to call
        repeatedly. Recovery order:

        1. ``claimed`` rows:
           - If a matching ``whatsapp_connections`` row exists → delete the
             pool row (the claim was finalized on the other side).
           - Else poll Green state:
             - ``notAuthorized`` → ``release_claim`` (return to available).
             - else → best-effort Green delete + delete row.
        2. ``creating`` rows with credentials (crash during ready poll):
           - Poll Green state:
             - ``notAuthorized`` → ``mark_available``.
             - ``authorized`` → best-effort Green delete + ``mark_failed``
               (an authorized instance is not a fresh slot).
             - else (timeout/unknown) → best-effort Green delete + ``mark_failed``.
        3. ``creating`` rows without credentials (crash during createInstance):
           - Delete row (createInstance didn't complete).
        4. ``failed`` rows with provider_id: best-effort Green delete + delete row.
        5. ``failed`` rows without provider_id: delete row.
        6. While ``reserve_creation_slot`` returns a row id: ``_create_one``.
        """
        await self._recover_claimed()
        await self._recover_creating_with_credentials()
        await self._recover_creating_without_credentials()
        await self._recover_failed()
        await self._fill_to_target()

    async def claim(self, user_id: str) -> PooledInstance | None:
        """Atomically claim one ``available`` row.

        Returns ``None`` if no instance is available. The caller must
        persist the connection to ``whatsapp_connections`` and then call
        :meth:`finalize_claim` to delete the pool row.
        """
        return await self._repo.claim(user_id)

    async def finalize_claim(self, pool_row_id: str) -> None:
        """Delete a ``claimed`` row after the connection has been persisted."""
        await self._repo.finalize_claim(pool_row_id)

    def request_refill(self) -> None:
        """Kick off a single background refill task if one isn't already running.

        Safe to call repeatedly: if a refill task is already in flight, this
        is a no-op. The task is tracked so it can be cancelled on shutdown.
        """
        if self._refill_task is not None and not self._refill_task.done():
            return
        self._refill_task = asyncio.create_task(self._refill_to_target())

    async def aclose(self) -> None:
        """Cancel the refill task on shutdown."""
        if self._refill_task is not None:
            self._refill_task.cancel()
            try:
                await self._refill_task
            except asyncio.CancelledError:
                pass
            self._refill_task = None

    # --- recovery ---------------------------------------------------------

    async def _recover_claimed(self) -> None:
        claimed = await self._repo.get_by_state("claimed")
        for row in claimed:
            if row.provider_connection_id is None:
                # Shouldn't happen (CHECK constraint), but be defensive.
                await self._repo.delete_row(row.id)
                continue
            conn = await self._connection_repo.get_by_provider_id(
                "green", row.provider_connection_id,
            )
            if conn is not None:
                # Claim was finalized on the connection side — delete pool row.
                _logger.info(
                    "pool recovery: claimed row %s has matching connection, "
                    "finalizing", row.id,
                )
                await self._repo.delete_row(row.id)
                continue
            # No matching connection — check Green state.
            if row.credentials is None:
                await self._repo.delete_row(row.id)
                continue
            api_token = row.credentials.data.decode("utf-8")
            state = await self._safe_get_state(
                row.provider_connection_id, api_token,
            )
            if state == "notAuthorized":
                _logger.info(
                    "pool recovery: releasing claimed row %s (still notAuthorized)",
                    row.id,
                )
                await self._repo.release_claim(row.id)
            else:
                _logger.info(
                    "pool recovery: claimed row %s state=%s, cleaning up",
                    row.id, state,
                )
                await self._best_effort_delete_green(row.provider_connection_id)
                await self._repo.delete_row(row.id)

    async def _recover_creating_with_credentials(self) -> None:
        rows = await self._repo.get_creating_with_credentials()
        for row in rows:
            if row.provider_connection_id is None or row.credentials is None:
                continue
            api_token = row.credentials.data.decode("utf-8")
            state = await self._safe_get_state(
                row.provider_connection_id, api_token,
            )
            if state == "notAuthorized":
                _logger.info(
                    "pool recovery: creating row %s ready, marking available",
                    row.id,
                )
                await self._repo.mark_available(row.id)
            else:
                _logger.info(
                    "pool recovery: creating row %s state=%s, marking failed",
                    row.id, state,
                )
                await self._best_effort_delete_green(row.provider_connection_id)
                await self._repo.mark_failed(row.id)

    async def _recover_creating_without_credentials(self) -> None:
        rows = await self._repo.get_creating_without_credentials()
        for row in rows:
            _logger.info(
                "pool recovery: deleting creating row %s (no credentials)",
                row.id,
            )
            await self._repo.delete_row(row.id)

    async def _recover_failed(self) -> None:
        # A failed row means the ready poll timed out. But Green's auth
        # propagation can be slow — the instance may have become ready
        # *after* we timed out. Re-check state before deleting:
        # - notAuthorized → mark available (recover the instance).
        # - authorized → best-effort delete + delete row (not a fresh slot).
        # - else (still failing / 401 / timeout) → best-effort delete + delete row.
        failed_with_id = await self._repo.get_failed_with_provider_id()
        for row in failed_with_id:
            if row.provider_connection_id is None or row.credentials is None:
                await self._repo.delete_row(row.id)
                continue
            api_token = row.credentials.data.decode("utf-8")
            state = await self._safe_get_state(
                row.provider_connection_id, api_token,
            )
            if state == "notAuthorized":
                _logger.info(
                    "pool recovery: failed row %s is now notAuthorized, "
                    "marking available (id=%s)",
                    row.id, row.provider_connection_id,
                )
                await self._repo.mark_available(row.id)
                continue
            # Still not ready — best-effort delete + delete row.
            _logger.info(
                "pool recovery: failed row %s still not ready (state=%s), "
                "cleaning up (id=%s)",
                row.id, state, row.provider_connection_id,
            )
            await self._best_effort_delete_green(row.provider_connection_id)
            await self._repo.delete_row(row.id)
        # Delete failed rows without provider id or credentials.
        all_failed = await self._repo.get_by_state("failed")
        for row in all_failed:
            await self._repo.delete_row(row.id)

    async def _fill_to_target(self) -> None:
        # Failed rows are NOT cleaned up here — they count toward capacity
        # (reserve_creation_slot counts creating + available + failed) so the
        # pool doesn't immediately re-create after a permanent failure.
        # Failed rows are cleaned up on ensure_capacity (startup recovery).
        while True:
            row_id = await self._repo.reserve_creation_slot(self._target_size)
            if row_id is None:
                return
            success = await self._create_one(row_id)
            if not success:
                # createInstance failed (e.g. Green 500). Back off before
                # the next attempt to avoid hammering Green's API in a
                # tight retry loop. The row was already deleted, so the
                # next iteration will reserve a fresh slot.
                await asyncio.sleep(self._create_backoff_seconds)

    # --- creation ---------------------------------------------------------

    async def _refill_to_target(self) -> None:
        """Create instances until target_size reached (background refill)."""
        try:
            await self._fill_to_target()
        except Exception:
            _logger.exception("pool: refill task failed")

    async def _create_one(self, row_id: str) -> bool:
        """Create a Green instance for a reserved ``creating`` row.

        Returns ``True`` if the instance was created and marked available
        (or marked failed after a ready-timeout). Returns ``False`` if
        ``createInstance`` itself failed (e.g. Green 500) — the caller
        should back off before retrying.

        Steps:
        1. Row is already ``creating`` (reserved by ``reserve_creation_slot``).
        2. Generate webhook token + config.
        3. Call ``provisioner.create_connection`` (Green ``createInstance``).
        4. On failure → delete row, return ``False``.
        5. Persist credentials via ``mark_created`` (still ``creating``).
        6. Poll until ``notAuthorized`` (``_wait_until_ready``).
        7. On success → ``mark_available``.
        8. On failure/timeout → best-effort Green delete + ``mark_failed``.
        """
        webhook_token = secrets.token_urlsafe(32)
        webhook_url = f"{self._webhook_base_url}/webhooks/whatsapp/green"
        config = ConnectionConfig(
            webhook_url=webhook_url,
            webhook_token=webhook_token,
            subscriptions=WhatsAppEventSubscription(),
        )

        try:
            created = await self._provisioner.create_connection(config)
        except Exception:
            _logger.exception("pool: createInstance failed for row %s", row_id)
            # No provider_connection_id was ever assigned, so mark_failed
            # would violate the green_pool_id_when_not_creating_check
            # constraint (state='failed' requires a non-null id). Delete
            # the row instead — the pool will refill on the next cycle.
            await self._repo.delete_row(row_id)
            return False

        token_hash = _sha256(webhook_token)
        await self._repo.mark_created(
            row_id, created.ref, created.credentials, token_hash,
        )
        _logger.info(
            "pool: instance created for row %s id=%s",
            row_id, created.ref.provider_connection_id,
        )

        api_token = created.credentials.data.decode("utf-8")
        ready = await self._wait_until_ready(
            created.ref.provider_connection_id, api_token,
        )
        if ready:
            await self._repo.mark_available(row_id)
            _logger.info(
                "pool: row %s available (id=%s)",
                row_id, created.ref.provider_connection_id,
            )
        else:
            _logger.warning(
                "pool: row %s not ready, cleaning up (id=%s)",
                row_id, created.ref.provider_connection_id,
            )
            await self._best_effort_delete_green(created.ref.provider_connection_id)
            await self._repo.mark_failed(row_id)
        return True

    async def _wait_until_ready(
        self, id_instance: str, api_token: str,
    ) -> bool:
        """Poll ``getStateInstance`` until ``notAuthorized``.

        Returns ``True`` on ``notAuthorized``. Returns ``False`` on timeout
        or ``authorized`` (an authorized instance is not a fresh slot — it's
        suspicious and should be cleaned up).

        A 401 immediately after ``createInstance`` is a Green propagation
        delay (the instance exists but Green's auth hasn't propagated yet),
        NOT a permanent auth error. So we keep polling through all exceptions
        until the timeout. The instance may become usable on a later attempt.
        """
        for _ in range(self._ready_max_attempts):
            try:
                state = await self._green_client.get_state_instance(
                    id_instance, api_token,
                )
                if state == "notAuthorized":
                    return True
                if state == "authorized":
                    _logger.warning(
                        "pool: instance %s is authorized during warmup "
                        "(not a fresh slot)", id_instance,
                    )
                    return False
            except Exception:
                _logger.debug(
                    "pool: transient getStateInstance error for %s, keep polling",
                    id_instance,
                    exc_info=True,
                )
            await asyncio.sleep(self._ready_poll_interval)
        return False

    # --- helpers ----------------------------------------------------------

    async def _safe_get_state(
        self, id_instance: str, api_token: str,
    ) -> str | None:
        try:
            return await self._green_client.get_state_instance(
                id_instance, api_token,
            )
        except Exception:
            _logger.exception(
                "pool recovery: getStateInstance failed for %s", id_instance,
            )
            return None

    async def _best_effort_delete_green(
        self, provider_connection_id: str,
    ) -> None:
        ref = ConnectionRef(
            provider="green", provider_connection_id=provider_connection_id,
        )
        try:
            await self._provisioner.delete_connection(ref)
        except Exception:
            _logger.exception(
                "pool: best-effort delete failed for %s", provider_connection_id,
            )


def _sha256(s: str) -> bytes:
    import hashlib

    return hashlib.sha256(s.encode("utf-8")).digest()
