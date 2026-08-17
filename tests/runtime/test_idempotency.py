import asyncio

import pytest

from echo_v2.runtime.context import RunContext
from echo_v2.runtime.errors import (
    IndeterminateError,
    PermanentError,
    RetryableError,
    TimeoutError,
)
from echo_v2.runtime.events import RuntimeEvent
from echo_v2.runtime.executor import execute
from echo_v2.runtime.idempotency import (
    IndeterminateOutcome,
    InMemoryIdempotencyStore,
    LostOwnershipError,
    PermanentFailureOutcome,
    ReserveStatus,
    SuccessOutcome,
)
from echo_v2.runtime.policy import ExecutionPolicy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecordingEventSink:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    def emit(self, event: RuntimeEvent) -> None:
        self.events.append(event)


def _ctx(operation_name: str = "op") -> RunContext:
    return RunContext(run_id="run-test", operation_name=operation_name)


# ---------------------------------------------------------------------------
# InMemoryIdempotencyStore unit tests
# ---------------------------------------------------------------------------


async def test_reserve_acquires_then_in_progress_then_completed():
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()

    r1 = await store.reserve("k")
    assert r1.status == ReserveStatus.ACQUIRED
    assert r1.owner_token is not None

    r2 = await store.reserve("k")
    assert r2.status == ReserveStatus.IN_PROGRESS
    assert r2.owner_token is None

    await store.put_success("k", r1.owner_token, SuccessOutcome(value=1, attempts=1, duration_ms=1.0))
    r3 = await store.reserve("k")
    assert r3.status == ReserveStatus.COMPLETED


async def test_get_returns_none_for_unknown_key():
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    assert await store.get("missing") is None


async def test_put_success_resolves_waiter():
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()

    r = await store.reserve("k")
    assert r.owner_token is not None

    async def waiter():
        return await store.wait_for_completion("k")

    wait_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)  # let waiter suspend on the future

    await store.put_success("k", r.owner_token, SuccessOutcome(value=42, attempts=1, duration_ms=1.0))

    outcome = await wait_task
    assert isinstance(outcome, SuccessOutcome)
    assert outcome.value == 42


async def test_put_failure_resolves_waiter():
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()

    r = await store.reserve("k")
    assert r.owner_token is not None

    async def waiter():
        return await store.wait_for_completion("k")

    wait_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)

    await store.put_failure(
        "k",
        r.owner_token,
        PermanentFailureOutcome(error_type="PermanentError", error_message="boom"),
    )

    outcome = await wait_task
    assert isinstance(outcome, PermanentFailureOutcome)
    assert outcome.error_message == "boom"


async def test_release_wakes_waiter_with_retryable_error():
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()

    r = await store.reserve("k")
    assert r.owner_token is not None

    async def waiter():
        return await store.wait_for_completion("k")

    wait_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)

    await store.release("k", r.owner_token)

    with pytest.raises(RetryableError):
        await wait_task


async def test_release_without_waiter_is_noop():
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    r = await store.reserve("k")
    await store.release("k", r.owner_token)  # no waiter, should not raise
    assert await store.get("k") is None


async def test_wait_for_completion_after_release_raises_retryable():
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    r = await store.reserve("k")
    await store.release("k", r.owner_token)

    with pytest.raises(RetryableError):
        await store.wait_for_completion("k")


async def test_wrong_owner_token_raises_lost_ownership():
    """A put_* with the wrong owner_token raises LostOwnershipError (fencing)."""
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()

    r = await store.reserve("k")
    assert r.owner_token is not None

    wrong_token = __import__("uuid").uuid4()
    with pytest.raises(LostOwnershipError):
        await store.put_success(
            "k",
            wrong_token,
            SuccessOutcome(value=1, attempts=1, duration_ms=1.0),
        )
    # The correct token still works after the failed wrong-token attempt.
    await store.put_success(
        "k",
        r.owner_token,
        SuccessOutcome(value=1, attempts=1, duration_ms=1.0),
    )
    assert isinstance(await store.get("k"), SuccessOutcome)


async def test_renew_lease_returns_true_for_owner_false_for_non_owner():
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()

    r = await store.reserve("k")
    assert r.owner_token is not None

    assert await store.renew_lease("k", r.owner_token) is True
    wrong_token = __import__("uuid").uuid4()
    assert await store.renew_lease("k", wrong_token) is False
    # After terminal, renew returns False even for the prior owner.
    await store.put_success(
        "k",
        r.owner_token,
        SuccessOutcome(value=1, attempts=1, duration_ms=1.0),
    )
    assert await store.renew_lease("k", r.owner_token) is False


# ---------------------------------------------------------------------------
# End-to-end tests through execute()
# ---------------------------------------------------------------------------


async def test_cached_success_returned_without_rerunning():
    sink = RecordingEventSink()
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    calls = 0

    def double(value: int) -> int:
        nonlocal calls
        calls += 1
        return value * 2

    first = await execute(
        operation=double,
        input_=21,
        context=_ctx(),
        idempotency_key="op:1",
        idempotency_store=store,
        event_sink=sink,
    )
    assert first.value == 42
    assert calls == 1

    sink.events.clear()
    second = await execute(
        operation=double,
        input_=21,
        context=_ctx(),
        idempotency_key="op:1",
        idempotency_store=store,
        event_sink=sink,
    )
    assert second.value == 42
    assert calls == 1  # operation not re-run

    assert [e.name for e in sink.events] == ["operation.idempotent.hit"]
    assert sink.events[0].attributes["idempotency_key"] == "op:1"


async def test_cached_permanent_failure_replayed_as_permanent_error():
    sink = RecordingEventSink()
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    calls = 0

    def fails(value: int) -> int:
        nonlocal calls
        calls += 1
        raise PermanentError("nope")

    with pytest.raises(PermanentError):
        await execute(
            operation=fails,
            input_=1,
            context=_ctx(),
            idempotency_key="op:fail",
            idempotency_store=store,
            event_sink=sink,
        )
    assert calls == 1

    sink.events.clear()
    with pytest.raises(PermanentError) as exc_info:
        await execute(
            operation=fails,
            input_=1,
            context=_ctx(),
            idempotency_key="op:fail",
            idempotency_store=store,
            event_sink=sink,
        )
    assert calls == 1  # not re-run
    assert "nope" in str(exc_info.value)

    assert [e.name for e in sink.events] == ["operation.idempotent.replayed"]


async def test_retryable_failure_not_cached_subsequent_call_reruns():
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    calls = 0

    def flaky(value: int) -> int:
        nonlocal calls
        calls += 1
        raise RetryableError("temporary")

    policy = ExecutionPolicy(max_attempts=2, retry_delay_seconds=0)

    with pytest.raises(RetryableError):
        await execute(
            operation=flaky,
            input_=1,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:retry",
            idempotency_store=store,
        )
    assert calls == 2  # exhausted retries on first call

    # Second call should re-run because retryable failures are not cached.
    with pytest.raises(RetryableError):
        await execute(
            operation=flaky,
            input_=1,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:retry",
            idempotency_store=store,
        )
    assert calls == 4


async def test_concurrent_duplicates_wait_and_share_result():
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    calls = 0
    started = asyncio.Event()

    async def slow_double(value: int) -> int:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.sleep(0.05)
        return value * 2

    async def call() -> int:
        result = await execute(
            operation=slow_double,
            input_=10,
            context=_ctx(),
            idempotency_key="op:concurrent",
            idempotency_store=store,
        )
        return result.value

    results = await asyncio.gather(call(), call(), call())
    assert results == [20, 20, 20]
    assert calls == 1  # operation ran exactly once
    await started.wait()


async def test_waiter_on_released_claim_gets_retryable_and_can_retry():
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    calls = 0

    def fail_once_then_succeed(value: int) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RetryableError("temporary")
        return value * 2

    policy = ExecutionPolicy(max_attempts=1, retry_delay_seconds=0)

    # First call: retryable failure -> claim released, not cached.
    with pytest.raises(RetryableError):
        await execute(
            operation=fail_once_then_succeed,
            input_=5,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:release",
            idempotency_store=store,
        )
    assert calls == 1

    # Second call: should re-run and succeed.
    result = await execute(
        operation=fail_once_then_succeed,
        input_=5,
        context=_ctx(),
        policy=policy,
        idempotency_key="op:release",
        idempotency_store=store,
    )
    assert result.value == 10
    assert calls == 2


async def test_owner_cancellation_releases_claim_and_waiter_wakes():
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    started = asyncio.Event()

    async def hanging(value: int) -> int:
        started.set()
        await asyncio.sleep(10)
        return value  # unreachable

    async def owner() -> None:
        await execute(
            operation=hanging,
            input_=1,
            context=_ctx(),
            idempotency_key="op:cancel",
            idempotency_store=store,
        )

    owner_task = asyncio.create_task(owner())
    await started.wait()

    async def waiter() -> int:
        result = await execute(
            operation=hanging,
            input_=1,
            context=_ctx(),
            idempotency_key="op:cancel",
            idempotency_store=store,
        )
        return result.value

    wait_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)  # let waiter suspend on the future

    owner_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner_task

    # Waiter should wake with RetryableError (released claim), not hang.
    with pytest.raises(RetryableError):
        await asyncio.wait_for(wait_task, timeout=1.0)


async def test_key_without_store_raises_value_error():
    with pytest.raises(ValueError):
        await execute(
            operation=lambda v: v,
            input_=1,
            context=_ctx(),
            idempotency_key="op:1",
        )


async def test_store_without_key_raises_value_error():
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    with pytest.raises(ValueError):
        await execute(
            operation=lambda v: v,
            input_=1,
            context=_ctx(),
            idempotency_store=store,
        )


async def test_cache_hit_emits_no_operation_started():
    sink = RecordingEventSink()
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()

    await execute(
        operation=lambda v: v * 2,
        input_=5,
        context=_ctx(),
        idempotency_key="op:events",
        idempotency_store=store,
        event_sink=sink,
    )

    sink.events.clear()
    await execute(
        operation=lambda v: v * 2,
        input_=5,
        context=_ctx(),
        idempotency_key="op:events",
        idempotency_store=store,
        event_sink=sink,
    )

    names = [e.name for e in sink.events]
    assert names == ["operation.idempotent.hit"]
    assert "operation.started" not in names


async def test_owner_path_emits_started_then_succeeded_with_key():
    sink = RecordingEventSink()
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()

    await execute(
        operation=lambda v: v * 2,
        input_=5,
        context=_ctx(),
        idempotency_key="op:owner",
        idempotency_store=store,
        event_sink=sink,
    )

    names = [e.name for e in sink.events]
    assert names == ["operation.started", "operation.succeeded"]
    assert sink.events[0].attributes["idempotency_key"] == "op:owner"
    assert sink.events[1].attributes["idempotency_key"] == "op:owner"


async def test_waiter_then_shared_success_emits_waiting_then_hit():
    sink = RecordingEventSink()
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    started = asyncio.Event()

    async def slow_double(value: int) -> int:
        started.set()
        await asyncio.sleep(0.02)
        return value * 2

    async def owner_call():
        return await execute(
            operation=slow_double,
            input_=7,
            context=_ctx(),
            idempotency_key="op:wait",
            idempotency_store=store,
            event_sink=sink,
        )

    async def waiter_call():
        # Ensure owner reserves first.
        await started.wait()
        return await execute(
            operation=slow_double,
            input_=7,
            context=_ctx(),
            idempotency_key="op:wait",
            idempotency_store=store,
            event_sink=sink,
        )

    owner_result, waiter_result = await asyncio.gather(
        owner_call(),
        waiter_call(),
    )
    assert owner_result.value == 14
    assert waiter_result.value == 14

    waiter_events = [e for e in sink.events if e.name == "operation.idempotent.waiting"]
    assert len(waiter_events) == 1
    hit_events = [e for e in sink.events if e.name == "operation.idempotent.hit"]
    assert len(hit_events) == 1
    # The waiter must not have emitted operation.started.
    started_events = [e for e in sink.events if e.name == "operation.started"]
    assert len(started_events) == 1  # only the owner


async def test_no_idempotency_zero_behavior_change():
    """Without idempotency params, execute() behaves exactly as before."""
    sink = RecordingEventSink()
    result = await execute(
        operation=lambda v: v * 2,
        input_=21,
        context=_ctx(),
        event_sink=sink,
    )
    assert result.value == 42
    assert result.attempts == 1
    assert [e.name for e in sink.events] == [
        "operation.started",
        "operation.succeeded",
    ]
    # No idempotency_key attribute on events when not used.
    assert "idempotency_key" not in sink.events[0].attributes


# ---------------------------------------------------------------------------
# Foundation Hardening 0.1: INDETERMINATE outcome taxonomy + single-emission
# ---------------------------------------------------------------------------


# Parameterized taxonomy test — encodes the D2 table so the design doc and
# implementation cannot quietly diverge. Each row runs execute() with a
# one-attempt policy, the given operation, and irreversible_write flag; then
# asserts the store ends in the expected state and the raised error matches.
#
# "timeout" rows use a real async timeout (sleep > timeout_seconds) to trigger
# asyncio.TimeoutError from wait_for, not a direct raise of our custom
# TimeoutError (which extends RetryableError and would go through that path).
def _make_op(kind: str):
    """Build an operation that fails in the given way."""
    if kind == "timeout":

        async def timeout_op(value: int) -> int:
            await asyncio.sleep(1.0)
            return value

        return timeout_op

    exc_map = {
        "permanent": lambda: PermanentError("nope"),
        "retryable": lambda: RetryableError("429"),
        "indeterminate": lambda: IndeterminateError("ambig"),
        "keyerror": lambda: KeyError("parse"),
    }

    def sync_op(value: int) -> int:
        raise exc_map[kind]()

    return sync_op


@pytest.mark.parametrize(
    "op_kind, irreversible, expected_outcome, expected_error",
    [
        ("permanent", False, "failure", PermanentError),
        ("permanent", True, "failure", PermanentError),
        ("retryable", False, "release", RetryableError),
        ("retryable", True, "release", RetryableError),
        ("timeout", False, "release", TimeoutError),
        # On first call, irreversible timeout raises TimeoutError (not
        # IndeterminateError); the IndeterminateError only appears on replay.
        ("timeout", True, "indeterminate", TimeoutError),
        ("indeterminate", False, "indeterminate", IndeterminateError),
        ("indeterminate", True, "indeterminate", IndeterminateError),
        ("keyerror", False, "failure", PermanentError),
        ("keyerror", True, "indeterminate", IndeterminateError),
    ],
)
async def test_idempotent_owner_outcome_taxonomy(
    op_kind, irreversible, expected_outcome, expected_error
):
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    operation = _make_op(op_kind)

    if op_kind == "timeout":
        policy = ExecutionPolicy(
            max_attempts=1,
            timeout_seconds=0.05,
            irreversible_write=irreversible,
        )
    else:
        policy = ExecutionPolicy(
            max_attempts=1,
            irreversible_write=irreversible,
        )

    with pytest.raises(expected_error):
        await execute(
            operation=operation,
            input_=1,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:taxonomy",
            idempotency_store=store,
        )

    stored = await store.get("op:taxonomy")
    if expected_outcome == "failure":
        assert isinstance(stored, PermanentFailureOutcome)
    elif expected_outcome == "indeterminate":
        assert isinstance(stored, IndeterminateOutcome)
    elif expected_outcome == "release":
        assert stored is None


async def test_indeterminate_outcome_blocks_re_execution():
    """Irreversible write times out → second call raises IndeterminateError,
    operation is NOT re-run."""
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    calls = 0

    async def slow(value: int) -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1.0)
        return value

    policy = ExecutionPolicy(
        max_attempts=1,
        timeout_seconds=0.05,
        irreversible_write=True,
    )

    with pytest.raises(TimeoutError):
        await execute(
            operation=slow,
            input_=10,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:indeterminate",
            idempotency_store=store,
        )
    assert calls == 1

    # Second call: must NOT re-run; must raise IndeterminateError.
    with pytest.raises(IndeterminateError):
        await execute(
            operation=slow,
            input_=10,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:indeterminate",
            idempotency_store=store,
        )
    assert calls == 1  # still not re-run


async def test_indeterminate_outcome_waiter_wakes_with_indeterminate_error():
    """Concurrent waiter on a key whose owner times out irreversibly wakes
    with IndeterminateError (not RetryableError)."""
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    started = asyncio.Event()

    async def slow(value: int) -> int:
        started.set()
        await asyncio.sleep(1.0)
        return value

    policy = ExecutionPolicy(
        max_attempts=1,
        timeout_seconds=0.05,
        irreversible_write=True,
    )

    async def owner():
        with pytest.raises(TimeoutError):
            await execute(
                operation=slow,
                input_=10,
                context=_ctx(),
                policy=policy,
                idempotency_key="op:waiter-indet",
                idempotency_store=store,
            )

    async def waiter():
        await started.wait()
        with pytest.raises(IndeterminateError):
            await execute(
                operation=slow,
                input_=10,
                context=_ctx(),
                policy=policy,
                idempotency_key="op:waiter-indet",
                idempotency_store=store,
            )

    await asyncio.gather(owner(), waiter())


async def test_reversible_write_timeout_releases_claim():
    """With irreversible_write=False, a timeout releases the key and a second
    call re-runs (preserves current behavior; regression guard)."""
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    calls = 0

    async def slow(value: int) -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1.0)
        return value

    policy = ExecutionPolicy(
        max_attempts=1,
        timeout_seconds=0.05,
        irreversible_write=False,
    )

    with pytest.raises(TimeoutError):
        await execute(
            operation=slow,
            input_=10,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:reversible-timeout",
            idempotency_store=store,
        )
    assert calls == 1
    assert await store.get("op:reversible-timeout") is None

    # Second call: should re-run (key was released).
    with pytest.raises(TimeoutError):
        await execute(
            operation=slow,
            input_=10,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:reversible-timeout",
            idempotency_store=store,
        )
    assert calls == 2


async def test_retryable_failure_on_irreversible_write_releases_claim():
    """With irreversible_write=True, a RetryableError (non-timeout, e.g. 429)
    still releases the key — do NOT over-block for unambiguous failures."""
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    calls = 0

    def flaky(value: int) -> int:
        nonlocal calls
        calls += 1
        raise RetryableError("429 rate limited")

    policy = ExecutionPolicy(
        max_attempts=1,
        irreversible_write=True,
    )

    with pytest.raises(RetryableError):
        await execute(
            operation=flaky,
            input_=10,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:retryable-irr",
            idempotency_store=store,
        )
    assert calls == 1
    assert await store.get("op:retryable-irr") is None

    # Second call: should re-run.
    with pytest.raises(RetryableError):
        await execute(
            operation=flaky,
            input_=10,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:retryable-irr",
            idempotency_store=store,
        )
    assert calls == 2


async def test_cancellation_of_irreversible_write_is_indeterminate():
    """With irreversible_write=True, cancelling the owner mid-flight stores
    IndeterminateOutcome; a second call raises IndeterminateError and does
    NOT re-run."""
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    started = asyncio.Event()
    calls = 0

    async def hanging(value: int) -> int:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.sleep(10)
        return value

    policy = ExecutionPolicy(
        max_attempts=1,
        irreversible_write=True,
    )

    async def owner():
        await execute(
            operation=hanging,
            input_=1,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:cancel-irr",
            idempotency_store=store,
        )

    owner_task = asyncio.create_task(owner())
    await started.wait()
    owner_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner_task

    assert calls == 1
    stored = await store.get("op:cancel-irr")
    assert isinstance(stored, IndeterminateOutcome)

    # Second call: must NOT re-run; must raise IndeterminateError.
    with pytest.raises(IndeterminateError):
        await execute(
            operation=hanging,
            input_=1,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:cancel-irr",
            idempotency_store=store,
        )
    assert calls == 1


async def test_cancellation_of_reversible_write_releases_claim():
    """With irreversible_write=False, cancelling the owner releases the key
    and a later call can re-run (preserves existing behavior)."""
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    started = asyncio.Event()
    calls = 0

    async def hanging(value: int) -> int:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.sleep(10)
        return value

    policy = ExecutionPolicy(
        max_attempts=1,
        irreversible_write=False,
    )

    async def owner():
        await execute(
            operation=hanging,
            input_=1,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:cancel-rev",
            idempotency_store=store,
        )

    owner_task = asyncio.create_task(owner())
    await started.wait()
    owner_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner_task

    assert calls == 1
    assert await store.get("op:cancel-rev") is None

    # Second call: should re-run (key was released).
    second_started = asyncio.Event()

    async def hanging2(value: int) -> int:
        nonlocal calls
        calls += 1
        second_started.set()
        await asyncio.sleep(10)
        return value

    second_task = asyncio.create_task(
        execute(
            operation=hanging2,
            input_=1,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:cancel-rev",
            idempotency_store=store,
        )
    )
    await second_started.wait()
    second_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second_task
    assert calls == 2


async def test_explicit_indeterminate_error_stores_indeterminate():
    """An operation that raises IndeterminateError directly (simulating an
    integration that knows the outcome is ambiguous) stores IndeterminateOutcome
    regardless of irreversible_write, and a second call raises IndeterminateError
    without re-running."""
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    calls = 0

    def ambiguous(value: int) -> int:
        nonlocal calls
        calls += 1
        raise IndeterminateError("connection dropped after submit")

    # Test with irreversible_write=False — explicit IndeterminateError is
    # indeterminate regardless of policy.
    policy = ExecutionPolicy(
        max_attempts=1,
        irreversible_write=False,
    )

    with pytest.raises(IndeterminateError):
        await execute(
            operation=ambiguous,
            input_=10,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:explicit-indet",
            idempotency_store=store,
        )
    assert calls == 1

    stored = await store.get("op:explicit-indet")
    assert isinstance(stored, IndeterminateOutcome)

    # Second call: must NOT re-run.
    with pytest.raises(IndeterminateError):
        await execute(
            operation=ambiguous,
            input_=10,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:explicit-indet",
            idempotency_store=store,
        )
    assert calls == 1


async def test_process_restart_simulation_already_completed_never_reruns():
    """Pre-seed the store with a SuccessOutcome (simulating a persistent store
    that survived a restart). A subsequent execute() must NOT re-run and must
    emit operation.idempotent.hit."""
    sink = RecordingEventSink()
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    calls = 0

    # Pre-seed as if a previous process completed this operation.
    store.seed_outcome(
        "op:completed",
        SuccessOutcome(value=42, attempts=1, duration_ms=1.0),
    )

    def double(value: int) -> int:
        nonlocal calls
        calls += 1
        return value * 2

    result = await execute(
        operation=double,
        input_=21,
        context=_ctx(),
        idempotency_key="op:completed",
        idempotency_store=store,
        event_sink=sink,
    )

    assert result.value == 42
    assert calls == 0  # never ran
    assert [e.name for e in sink.events] == ["operation.idempotent.hit"]


async def test_replay_indeterminate_raises_indeterminate_error_and_emits_event():
    """Pre-seed with IndeterminateOutcome; assert IndeterminateError raised,
    operation.idempotent.indeterminate emitted (not .replayed), and no
    operation.started emitted."""
    sink = RecordingEventSink()
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()

    store.seed_outcome(
        "op:indet-cached",
        IndeterminateOutcome(
            error_type="TimeoutError",
            error_message="previously timed out",
        ),
    )

    def double(value: int) -> int:
        return value * 2

    with pytest.raises(IndeterminateError) as exc_info:
        await execute(
            operation=double,
            input_=21,
            context=_ctx(),
            idempotency_key="op:indet-cached",
            idempotency_store=store,
            event_sink=sink,
        )
    assert "previously timed out" in str(exc_info.value)

    names = [e.name for e in sink.events]
    assert names == ["operation.idempotent.indeterminate"]
    assert "operation.started" not in names
    assert "operation.idempotent.replayed" not in names


async def test_indeterminate_outcome_emits_indeterminate_event():
    """On the owner path, an irreversible-write timeout emits exactly
    operation.indeterminate (not operation.failed, and not both). Pins the
    single-emission rule end-to-end through execute()."""
    sink = RecordingEventSink()
    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()

    async def slow(value: int) -> int:
        await asyncio.sleep(1.0)
        return value

    policy = ExecutionPolicy(
        max_attempts=1,
        timeout_seconds=0.05,
        irreversible_write=True,
    )

    with pytest.raises(TimeoutError):
        await execute(
            operation=slow,
            input_=10,
            context=_ctx(),
            policy=policy,
            idempotency_key="op:indet-event",
            idempotency_store=store,
            event_sink=sink,
        )

    names = [e.name for e in sink.events]
    assert names == ["operation.started", "operation.indeterminate"]
    assert "operation.failed" not in names

    indet_event = sink.events[-1]
    assert indet_event.attributes["idempotency_key"] == "op:indet-event"
    assert indet_event.attributes["error_type"] == "TimeoutError"
