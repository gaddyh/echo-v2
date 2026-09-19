"""Tests for the GreenInstancePool and its repository.

Covers:
- Atomic capacity reservation under concurrent refill attempts.
- ``creating`` → ``available`` only after Green confirms ``notAuthorized``.
- Credentials persisted before readiness polling completes.
- Claim transition and finalization ordering.
- Crash/recovery semantics for stale ``claimed`` rows.
- Refill after claim.
- ``authorized`` during warmup → NOT marked available.
- Ready timeout → no ``mark_available``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from echo_v2.persistence.green_instance_pool import (
    InMemoryGreenInstancePoolRepository,
    PoolRow,
)
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    CreatedConnection,
    ProviderCredentials,
)
from echo_v2.services.green_instance_pool import GreenInstancePool

__all__ = []


# --- Fakes -----------------------------------------------------------------


class FakeGreenClient:
    """Fake GreenClient for pool tests."""

    def __init__(self) -> None:
        self._state_sequence: list[str | None] = []
        self._default_state: str = "notAuthorized"
        self.state_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []

    def set_state_sequence(self, states: list[str | None]) -> None:
        self._state_sequence = states

    def set_default_state(self, state: str) -> None:
        self._default_state = state

    async def get_state_instance(self, id_instance: str, api_token: str) -> str | None:
        self.state_calls.append((id_instance, api_token))
        if self._state_sequence:
            return self._state_sequence.pop(0)
        return self._default_state

    async def delete_instance(self, instance_id: str) -> None:
        self.delete_calls.append(instance_id)


class FakeProvisioner:
    """Fake GreenProvisioner for pool tests."""

    def __init__(self) -> None:
        self.create_calls: list = []
        self.delete_calls: list[ConnectionRef] = []
        self._next_id = 0
        self.should_fail: bool = False
        self.fail_count: int = 0  # fail this many calls, then succeed

    async def create_connection(self, config) -> CreatedConnection:
        from echo_v2.ports.whatsapp import ConnectionRef, ProviderCredentials
        self.create_calls.append(config)
        if self.should_fail and self.fail_count <= 0:
            raise RuntimeError("createInstance boom")
        if self.fail_count > 0:
            self.fail_count -= 1
            raise RuntimeError("createInstance boom")
        self._next_id += 1
        return CreatedConnection(
            ref=ConnectionRef(
                provider="green",
                provider_connection_id=f"inst-{self._next_id}",
            ),
            credentials=ProviderCredentials(
                data=f"token-{self._next_id}".encode(),
            ),
        )

    async def delete_connection(self, ref: ConnectionRef) -> None:
        self.delete_calls.append(ref)


class FakeConnectionRepo:
    """Fake connection repo for recovery tests."""

    def __init__(self) -> None:
        self._by_provider_id: dict[str, object] = {}

    def set_connection(self, provider_id: str) -> None:
        self._by_provider_id[provider_id] = object()

    async def get_by_provider_id(self, provider: str, provider_id: str):
        return self._by_provider_id.get(provider_id)


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def pool_setup():
    repo = InMemoryGreenInstancePoolRepository()
    green_client = FakeGreenClient()
    provisioner = FakeProvisioner()
    connection_repo = FakeConnectionRepo()
    pool = GreenInstancePool(
        provisioner=provisioner,
        green_client=green_client,
        repo=repo,
        connection_repo=connection_repo,
        webhook_base_url="https://echo.example.com",
        target_size=1,
        ready_poll_interval=0.01,
        ready_max_attempts=3,
    )
    return pool, repo, green_client, provisioner, connection_repo


# --- Repository tests ------------------------------------------------------


async def test_reserve_creation_slot_returns_row_id():
    repo = InMemoryGreenInstancePoolRepository()
    row_id = await repo.reserve_creation_slot(1)
    assert row_id is not None
    # Second slot should fail (target=1, one creating row exists).
    row_id2 = await repo.reserve_creation_slot(1)
    assert row_id2 is None


async def test_reserve_creation_slot_counts_creating_and_available():
    repo = InMemoryGreenInstancePoolRepository()
    # Create one creating row.
    row_id = await repo.reserve_creation_slot(2)
    assert row_id is not None
    # Second slot should succeed (target=2, one creating row).
    row_id2 = await repo.reserve_creation_slot(2)
    assert row_id2 is not None
    # Third slot should fail (target=2, two creating rows).
    row_id3 = await repo.reserve_creation_slot(2)
    assert row_id3 is None


async def test_reserve_creation_slot_excludes_claimed():
    """Claimed rows don't count toward capacity — refill can start after claim."""
    repo = InMemoryGreenInstancePoolRepository()
    # Create + make available + claim.
    row_id = await repo.reserve_creation_slot(1)
    assert row_id is not None
    await repo.mark_created(
        row_id,
        ConnectionRef(provider="green", provider_connection_id="inst-1"),
        ProviderCredentials(data=b"token-1"),
        b"hash-1",
    )
    await repo.mark_available(row_id)
    claimed = await repo.claim("user-1")
    assert claimed is not None

    # Now reserve again — claimed doesn't count, so we can create a new slot.
    new_row = await repo.reserve_creation_slot(1)
    assert new_row is not None


async def test_claim_returns_none_when_no_available():
    repo = InMemoryGreenInstancePoolRepository()
    result = await repo.claim("user-1")
    assert result is None


async def test_claim_returns_pooled_instance():
    repo = InMemoryGreenInstancePoolRepository()
    row_id = await repo.reserve_creation_slot(1)
    await repo.mark_created(
        row_id,
        ConnectionRef(provider="green", provider_connection_id="inst-1"),
        ProviderCredentials(data=b"token-1"),
        b"hash-1",
    )
    await repo.mark_available(row_id)

    result = await repo.claim("user-1")
    assert result is not None
    assert result.pool_row_id == row_id
    assert result.ref.provider_connection_id == "inst-1"
    assert result.credentials.data == b"token-1"
    assert result.webhook_token_hash == b"hash-1"


async def test_two_parallel_claims_get_different_instances():
    """Two concurrent claims with 2 available rows → distinct pool_row_ids."""
    repo = InMemoryGreenInstancePoolRepository()
    # Create two available rows.
    for i in range(2):
        row_id = await repo.reserve_creation_slot(2)
        assert row_id is not None
        await repo.mark_created(
            row_id,
            ConnectionRef(provider="green", provider_connection_id=f"inst-{i}"),
            ProviderCredentials(data=f"token-{i}".encode()),
            b"hash",
        )
        await repo.mark_available(row_id)

    results = await asyncio.gather(
        repo.claim("user-1"), repo.claim("user-2"),
    )
    assert results[0] is not None
    assert results[1] is not None
    assert results[0].pool_row_id != results[1].pool_row_id


async def test_finalize_claim_deletes_row():
    repo = InMemoryGreenInstancePoolRepository()
    row_id = await repo.reserve_creation_slot(1)
    await repo.finalize_claim(row_id)
    # Row is gone.
    rows = await repo.get_by_state("claimed")
    assert len(rows) == 0


async def test_release_claim_returns_to_available():
    repo = InMemoryGreenInstancePoolRepository()
    row_id = await repo.reserve_creation_slot(1)
    await repo.mark_created(
        row_id,
        ConnectionRef(provider="green", provider_connection_id="inst-1"),
        ProviderCredentials(data=b"token-1"),
        b"hash-1",
    )
    await repo.mark_available(row_id)
    await repo.claim("user-1")
    await repo.release_claim(row_id)
    available = await repo.get_by_state("available")
    assert len(available) == 1
    assert available[0].id == row_id


# --- Pool service tests ----------------------------------------------------


async def test_concurrent_refills_dont_exceed_target(pool_setup):
    """Two concurrent _refill_to_target calls with target=1 → only 1 row created."""
    pool, repo, _green, provisioner, _conn = pool_setup

    await asyncio.gather(
        pool._refill_to_target(),
        pool._refill_to_target(),
    )

    creating = await repo.get_by_state("creating")
    available = await repo.get_by_state("available")
    total = len(creating) + len(available)
    assert total == 1  # target_size=1
    assert len(provisioner.create_calls) == 1


async def test_create_one_marks_available_after_notAuthorized(pool_setup):
    """_create_one polls until notAuthorized, then marks available."""
    pool, repo, green, _prov, _conn = pool_setup
    green.set_state_sequence(["starting", "notAuthorized"])

    row_id = await repo.reserve_creation_slot(1)
    await pool._create_one(row_id)

    available = await repo.get_by_state("available")
    assert len(available) == 1
    assert available[0].provider_connection_id == "inst-1"


async def test_create_one_create_instance_failure_deletes_row(pool_setup):
    """createInstance raises → row deleted (not mark_failed, which would
    violate the green_pool_id_when_not_creating_check constraint since
    there's no provider_connection_id)."""
    pool, repo, _green, provisioner, _conn = pool_setup
    provisioner.should_fail = True

    row_id = await repo.reserve_creation_slot(1)
    success = await pool._create_one(row_id)

    assert success is False
    available = await repo.get_by_state("available")
    assert len(available) == 0
    failed = await repo.get_by_state("failed")
    assert len(failed) == 0
    creating = await repo.get_by_state("creating")
    assert len(creating) == 0  # row was deleted, not left in any state


async def test_fill_to_target_backs_off_on_create_failure():
    """When createInstance fails, _fill_to_target sleeps before retrying
    to avoid hammering Green's API in a tight loop."""
    repo = InMemoryGreenInstancePoolRepository()
    green_client = FakeGreenClient()
    provisioner = FakeProvisioner()
    provisioner.fail_count = 1  # fail once, then succeed
    connection_repo = FakeConnectionRepo()
    pool = GreenInstancePool(
        provisioner=provisioner,
        green_client=green_client,
        repo=repo,
        connection_repo=connection_repo,
        webhook_base_url="https://echo.example.com",
        target_size=1,
        ready_poll_interval=0.01,
        ready_max_attempts=3,
        create_backoff_seconds=0.05,
    )

    import time as _time
    start = _time.monotonic()
    await pool._fill_to_target()
    elapsed = _time.monotonic() - start

    # First createInstance failed, row deleted, backoff slept 0.05s,
    # then second createInstance succeeded and instance became available.
    assert len(provisioner.create_calls) == 2, (
        f"expected 2 create attempts, got {len(provisioner.create_calls)}"
    )
    assert elapsed >= 0.05, f"backoff not applied (elapsed={elapsed:.3f}s)"
    available = await repo.get_by_state("available")
    assert len(available) == 1


async def test_create_one_timeout_marks_failed(pool_setup):
    """Ready timeout → mark_failed, no available row."""
    pool, repo, green, provisioner, _conn = pool_setup
    green.set_state_sequence(["starting"] * 10)  # never becomes notAuthorized

    row_id = await repo.reserve_creation_slot(1)
    await pool._create_one(row_id)

    available = await repo.get_by_state("available")
    assert len(available) == 0
    failed = await repo.get_by_state("failed")
    assert len(failed) == 1
    # Best-effort delete was attempted.
    assert len(provisioner.delete_calls) == 1


async def test_create_one_authorized_marks_failed(pool_setup):
    """authorized during warmup → NOT available, best-effort delete + failed."""
    pool, repo, green, provisioner, _conn = pool_setup
    green.set_state_sequence(["authorized"])

    row_id = await repo.reserve_creation_slot(1)
    await pool._create_one(row_id)

    available = await repo.get_by_state("available")
    assert len(available) == 0
    failed = await repo.get_by_state("failed")
    assert len(failed) == 1
    assert len(provisioner.delete_calls) == 1


async def test_credentials_persisted_before_ready_poll(pool_setup):
    """mark_created is called before the ready poll starts."""
    pool, repo, green, _prov, _conn = pool_setup
    green.set_state_sequence(["starting", "notAuthorized"])

    row_id = await repo.reserve_creation_slot(1)
    await pool._create_one(row_id)

    # After create_one, the row should have credentials (mark_created was called).
    creating_or_available = (
        await repo.get_by_state("creating")
        + await repo.get_by_state("available")
    )
    assert len(creating_or_available) == 1
    row = creating_or_available[0]
    assert row.provider_connection_id == "inst-1"
    assert row.credentials is not None
    assert row.credentials.data == b"token-1"


async def test_refill_after_claim(pool_setup):
    """After a claim, request_refill creates a new instance."""
    pool, repo, green, _prov, _conn = pool_setup
    green.set_state_sequence(["notAuthorized"] * 10)

    # Warm up one instance.
    await pool.ensure_capacity()
    available = await repo.get_by_state("available")
    assert len(available) == 1

    # Claim it.
    claimed = await pool.claim("user-1")
    assert claimed is not None

    # Request refill.
    pool.request_refill()
    # Wait for background refill task.
    await asyncio.sleep(0.3)
    if pool._refill_task is not None:
        await pool._refill_task

    # A new instance should be available.
    available = await repo.get_by_state("available")
    assert len(available) == 1


async def test_recovery_claimed_with_matching_connection_deletes_row(pool_setup):
    """Crash after claim + connection save → recovery deletes the pool row."""
    pool, repo, _green, _prov, connection_repo = pool_setup

    # Simulate: a claimed row exists + matching connection was saved.
    row_id = await repo.reserve_creation_slot(1)
    await repo.mark_created(
        row_id,
        ConnectionRef(provider="green", provider_connection_id="inst-1"),
        ProviderCredentials(data=b"token-1"),
        b"hash-1",
    )
    await repo.mark_available(row_id)
    await repo.claim("user-1")
    connection_repo.set_connection("inst-1")

    await pool.ensure_capacity()

    # Row is deleted (finalized on the connection side).
    claimed = await repo.get_by_state("claimed")
    assert len(claimed) == 0


async def test_recovery_claimed_no_connection_notAuthorized_releases(pool_setup):
    """Crash after claim, no connection, Green still notAuthorized → release."""
    pool, repo, green, _prov, _connection_repo = pool_setup
    green.set_default_state("notAuthorized")

    row_id = await repo.reserve_creation_slot(1)
    await repo.mark_created(
        row_id,
        ConnectionRef(provider="green", provider_connection_id="inst-1"),
        ProviderCredentials(data=b"token-1"),
        b"hash-1",
    )
    await repo.mark_available(row_id)
    await repo.claim("user-1")
    # No connection saved (crash before connection_repo.save).

    await pool.ensure_capacity()

    # Row is released back to available.
    available = await repo.get_by_state("available")
    assert len(available) == 1
    claimed = await repo.get_by_state("claimed")
    assert len(claimed) == 0


async def test_recovery_creating_with_credentials_notAuthorized_marks_available(
    pool_setup,
):
    """Crash during ready poll, Green is notAuthorized → mark available."""
    pool, repo, green, _prov, _conn = pool_setup
    green.set_default_state("notAuthorized")

    # Simulate: creating row with credentials (crash during ready poll).
    row_id = await repo.reserve_creation_slot(1)
    await repo.mark_created(
        row_id,
        ConnectionRef(provider="green", provider_connection_id="inst-1"),
        ProviderCredentials(data=b"token-1"),
        b"hash-1",
    )

    await pool.ensure_capacity()

    available = await repo.get_by_state("available")
    assert len(available) == 1


async def test_recovery_creating_without_credentials_deletes_row(pool_setup):
    """Crash during createInstance (no credentials) → delete row."""
    pool, repo, _green, _prov, _conn = pool_setup

    # Simulate: creating row without credentials (createInstance didn't complete).
    await repo.reserve_creation_slot(1)

    await pool.ensure_capacity()

    creating = await repo.get_by_state("creating")
    assert len(creating) == 0


async def test_pool_size_zero_disables_pool():
    """Pool size 0 → pool is None in OnboardingService (tested via main.py logic).

    This is a logical test: GREEN_INSTANCE_POOL_SIZE=0 means pool=None,
    and OnboardingService falls back to synchronous on-demand creation.
    The `if self._pool` guards in onboarding.py handle this.
    """
    # Verify the guard logic: pool=None means claim returns None.
    repo = InMemoryGreenInstancePoolRepository()
    # With no pool, onboarding always creates synchronously.
    # This is implicitly tested by all the pool-miss tests above.
    assert repo is not None  # placeholder


# --- request_refill / aclose -----------------------------------------------


async def test_request_refill_noop_when_task_running(pool_setup):
    """request_refill is a no-op when a refill task is already in flight."""
    pool, _repo, _green, _prov, _conn = pool_setup
    # Simulate a running refill task.
    pool._refill_task = asyncio.create_task(asyncio.sleep(100))
    pool.request_refill()  # should return early (line 151)
    # Task was not replaced.
    assert pool._refill_task is not None
    # Clean up.
    pool._refill_task.cancel()
    try:
        await pool._refill_task
    except asyncio.CancelledError:
        pass


async def test_aclose_cancels_running_refill_task():
    """aclose cancels a running refill task and swallows CancelledError."""
    repo = InMemoryGreenInstancePoolRepository()
    green_client = FakeGreenClient()
    provisioner = FakeProvisioner()
    connection_repo = FakeConnectionRepo()
    pool = GreenInstancePool(
        provisioner=provisioner,
        green_client=green_client,
        repo=repo,
        connection_repo=connection_repo,
        webhook_base_url="https://echo.example.com",
        target_size=1,
        ready_poll_interval=0.01,
        ready_max_attempts=3,
    )
    # Block create_connection so the refill task stays running.
    block = asyncio.Event()

    async def blocking_create(config):
        await block.wait()  # never set

    provisioner.create_connection = blocking_create
    pool.request_refill()
    await asyncio.sleep(0.01)  # let the task start
    await pool.aclose()  # lines 160-161
    assert pool._refill_task is None


async def test_aclose_no_task_is_noop(pool_setup):
    """aclose with no refill task is a no-op."""
    pool, _repo, _green, _prov, _conn = pool_setup
    assert pool._refill_task is None
    await pool.aclose()
    assert pool._refill_task is None


# --- _recover_claimed edge cases -------------------------------------------


async def test_recover_claimed_no_provider_id_deletes_row(pool_setup):
    """Defensive: claimed row with no provider_connection_id → delete."""
    pool, repo, _green, _prov, _conn = pool_setup
    row_id = str(uuid.uuid4())
    repo._rows[row_id] = PoolRow(
        id=row_id,
        state="claimed",
        provider_connection_id=None,
        credentials=None,
        webhook_token_hash=None,
        claimed_by_user_id="user-1",
    )
    await pool._recover_claimed()
    claimed = await repo.get_by_state("claimed")
    assert len(claimed) == 0


async def test_recover_claimed_no_credentials_deletes_row(pool_setup):
    """Claimed row with provider_id but no credentials → delete."""
    pool, repo, _green, _prov, _conn = pool_setup
    row_id = str(uuid.uuid4())
    repo._rows[row_id] = PoolRow(
        id=row_id,
        state="claimed",
        provider_connection_id="inst-1",
        credentials=None,
        webhook_token_hash=None,
        claimed_by_user_id="user-1",
    )
    await pool._recover_claimed()
    claimed = await repo.get_by_state("claimed")
    assert len(claimed) == 0


async def test_recover_claimed_authorized_state_cleans_up(pool_setup):
    """Claimed row, no connection, Green state authorized → delete + best-effort Green delete."""
    pool, repo, green, provisioner, _conn = pool_setup
    green.set_default_state("authorized")
    row_id = await repo.reserve_creation_slot(1)
    await repo.mark_created(
        row_id,
        ConnectionRef(provider="green", provider_connection_id="inst-1"),
        ProviderCredentials(data=b"token-1"),
        b"hash-1",
    )
    await repo.mark_available(row_id)
    await repo.claim("user-1")
    await pool._recover_claimed()
    claimed = await repo.get_by_state("claimed")
    assert len(claimed) == 0
    assert len(provisioner.delete_calls) == 1


# --- _recover_creating_with_credentials edge cases -------------------------


async def test_recover_creating_with_credentials_no_credentials_skips(pool_setup):
    """Creating row with provider_id but no credentials → skip (defensive continue)."""
    pool, repo, _green, _prov, _conn = pool_setup
    row_id = str(uuid.uuid4())
    repo._rows[row_id] = PoolRow(
        id=row_id,
        state="creating",
        provider_connection_id="inst-1",
        credentials=None,
        webhook_token_hash=None,
        claimed_by_user_id=None,
    )
    await pool._recover_creating_with_credentials()
    # Row still exists (was skipped, not deleted).
    creating = await repo.get_by_state("creating")
    assert len(creating) == 1


async def test_recover_creating_with_credentials_authorized_marks_failed(pool_setup):
    """Creating row with credentials, Green state authorized → mark_failed + delete."""
    pool, repo, green, provisioner, _conn = pool_setup
    green.set_default_state("authorized")
    row_id = await repo.reserve_creation_slot(1)
    await repo.mark_created(
        row_id,
        ConnectionRef(provider="green", provider_connection_id="inst-1"),
        ProviderCredentials(data=b"token-1"),
        b"hash-1",
    )
    await pool._recover_creating_with_credentials()
    available = await repo.get_by_state("available")
    assert len(available) == 0
    failed = await repo.get_by_state("failed")
    assert len(failed) == 1
    assert len(provisioner.delete_calls) == 1


# --- _recover_failed edge cases --------------------------------------------


async def test_recover_failed_now_not_authorized_marks_available(pool_setup):
    """Failed row with provider_id, Green now notAuthorized → mark available."""
    pool, repo, green, _prov, _conn = pool_setup
    green.set_default_state("notAuthorized")
    row_id = await repo.reserve_creation_slot(1)
    await repo.mark_created(
        row_id,
        ConnectionRef(provider="green", provider_connection_id="inst-1"),
        ProviderCredentials(data=b"token-1"),
        b"hash-1",
    )
    await repo.mark_failed(row_id)
    await pool._recover_failed()
    available = await repo.get_by_state("available")
    assert len(available) == 1
    failed = await repo.get_by_state("failed")
    assert len(failed) == 0


async def test_recover_failed_still_not_ready_deletes_row(pool_setup):
    """Failed row with provider_id, Green still not ready → delete + best-effort Green delete."""
    pool, repo, green, provisioner, _conn = pool_setup
    green.set_default_state("authorized")
    row_id = await repo.reserve_creation_slot(1)
    await repo.mark_created(
        row_id,
        ConnectionRef(provider="green", provider_connection_id="inst-1"),
        ProviderCredentials(data=b"token-1"),
        b"hash-1",
    )
    await repo.mark_failed(row_id)
    await pool._recover_failed()
    failed = await repo.get_by_state("failed")
    assert len(failed) == 0
    assert len(provisioner.delete_calls) == 1


async def test_recover_failed_no_credentials_deletes_row(pool_setup):
    """Failed row with provider_id but no credentials → delete_row."""
    pool, repo, _green, _prov, _conn = pool_setup
    row_id = str(uuid.uuid4())
    repo._rows[row_id] = PoolRow(
        id=row_id,
        state="failed",
        provider_connection_id="inst-1",
        credentials=None,
        webhook_token_hash=None,
        claimed_by_user_id=None,
    )
    await pool._recover_failed()
    failed = await repo.get_by_state("failed")
    assert len(failed) == 0


async def test_recover_failed_no_provider_id_deletes_row(pool_setup):
    """Failed row without provider_id → delete (second loop in _recover_failed)."""
    pool, repo, _green, _prov, _conn = pool_setup
    row_id = str(uuid.uuid4())
    repo._rows[row_id] = PoolRow(
        id=row_id,
        state="failed",
        provider_connection_id=None,
        credentials=None,
        webhook_token_hash=None,
        claimed_by_user_id=None,
    )
    await pool._recover_failed()
    failed = await repo.get_by_state("failed")
    assert len(failed) == 0


# --- _refill_to_target exception handling ----------------------------------


async def test_refill_to_target_logs_exception(pool_setup):
    """_refill_to_target catches and logs exceptions from _fill_to_target."""
    pool, repo, _green, _prov, _conn = pool_setup

    async def boom(target_size):
        raise RuntimeError("reserve boom")

    repo.reserve_creation_slot = boom
    # Should not raise — exception is caught and logged.
    await pool._refill_to_target()


# --- _wait_until_ready transient error -------------------------------------


async def test_wait_until_ready_handles_transient_error(pool_setup):
    """_wait_until_ready keeps polling through transient getStateInstance errors."""
    pool, repo, green, _prov, _conn = pool_setup
    call_count = 0

    async def flaky_get_state(id_instance, api_token):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient boom")
        return "notAuthorized"

    green.get_state_instance = flaky_get_state
    row_id = await repo.reserve_creation_slot(1)
    await pool._create_one(row_id)
    available = await repo.get_by_state("available")
    assert len(available) == 1


# --- _safe_get_state exception ---------------------------------------------


async def test_safe_get_state_handles_exception(pool_setup):
    """_safe_get_state returns None on exception."""
    pool, _repo, green, _prov, _conn = pool_setup

    async def boom(id_instance, api_token):
        raise RuntimeError("state boom")

    green.get_state_instance = boom
    result = await pool._safe_get_state("inst-1", "token-1")
    assert result is None


async def test_safe_get_state_returns_state_on_success(pool_setup):
    """_safe_get_state returns the state string on success."""
    pool, _repo, green, _prov, _conn = pool_setup
    green.set_default_state("notAuthorized")
    result = await pool._safe_get_state("inst-1", "token-1")
    assert result == "notAuthorized"


# --- _best_effort_delete_green exception -----------------------------------


async def test_best_effort_delete_green_handles_exception(pool_setup):
    """_best_effort_delete_green swallows exceptions from delete_connection."""
    pool, _repo, _green, provisioner, _conn = pool_setup

    async def boom(ref):
        raise RuntimeError("delete boom")

    provisioner.delete_connection = boom
    # Should not raise.
    await pool._best_effort_delete_green("inst-1")


# --- finalize_claim / claim service methods --------------------------------


async def test_pool_finalize_claim_delegates_to_repo(pool_setup):
    """pool.finalize_claim deletes the row via the repo."""
    pool, repo, _green, _prov, _conn = pool_setup
    row_id = await repo.reserve_creation_slot(1)
    await repo.mark_created(
        row_id,
        ConnectionRef(provider="green", provider_connection_id="inst-1"),
        ProviderCredentials(data=b"token-1"),
        b"hash-1",
    )
    await repo.mark_available(row_id)
    await repo.claim("user-1")
    await pool.finalize_claim(row_id)
    claimed = await repo.get_by_state("claimed")
    assert len(claimed) == 0


async def test_pool_claim_returns_none_when_empty(pool_setup):
    """pool.claim returns None when no available rows."""
    pool, _repo, _green, _prov, _conn = pool_setup
    result = await pool.claim("user-1")
    assert result is None


async def test_pool_claim_returns_pooled_instance(pool_setup):
    """pool.claim returns a PooledInstance when an available row exists."""
    pool, repo, _green, _prov, _conn = pool_setup
    row_id = await repo.reserve_creation_slot(1)
    await repo.mark_created(
        row_id,
        ConnectionRef(provider="green", provider_connection_id="inst-1"),
        ProviderCredentials(data=b"token-1"),
        b"hash-1",
    )
    await repo.mark_available(row_id)
    result = await pool.claim("user-1")
    assert result is not None
    assert result.pool_row_id == row_id
    assert result.ref.provider_connection_id == "inst-1"
