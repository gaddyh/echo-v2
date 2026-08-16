import asyncio
import time

import pytest

from echo_v2.observability import InMemoryEventSink
from echo_v2.runtime.context import RunContext
from echo_v2.runtime.errors import (
    ApplicationError,
    IndeterminateError,
    PermanentError,
    RetryableError,
)
from echo_v2.runtime.executor import execute
from echo_v2.runtime.policy import ExecutionPolicy


async def test_execute_returns_operation_result():
    context = RunContext(operation_name="operation")

    def double(value: int) -> int:
        return value * 2

    result = await execute(
        operation=double,
        input_=21,
        context=context,
    )

    assert result.value == 42


async def test_execute_measures_duration():
    context = RunContext(operation_name="operation")

    def slow_operation(value: str) -> str:
        time.sleep(0.01)
        return value

    result = await execute(
        operation=slow_operation,
        input_="hello",
        context=context,
    )

    assert result.value == "hello"
    assert result.duration_ms >= 10


async def test_execute_preserves_application_error():
    context = RunContext(operation_name="operation")

    def operation(value: str) -> str:
        raise ApplicationError("bad input")

    with pytest.raises(ApplicationError):
        await execute(
            operation=operation,
            input_="hello",
            context=context,
        )


async def test_execute_wraps_unexpected_error():
    context = RunContext(operation_name="operation")

    def operation(value: str) -> str:
        raise KeyError("boom")

    with pytest.raises(PermanentError):
        await execute(
            operation=operation,
            input_="hello",
            context=context,
        )


async def test_execute_accepts_policy():
    context = RunContext(operation_name="operation")

    policy = ExecutionPolicy(
        max_attempts=3,
        timeout_seconds=2.0,
    )

    result = await execute(
        operation=lambda value: value * 2,
        input_=10,
        context=context,
        policy=policy,
    )

    assert result.value == 20


async def test_execute_preserves_retryable_error():
    context = RunContext(operation_name="operation")

    def operation(value: str) -> str:
        raise RetryableError("temporary failure")

    with pytest.raises(RetryableError):
        await execute(
            operation=operation,
            input_="hello",
            context=context,
        )


async def test_execute_retries_retryable_error():
    context = RunContext(operation_name="operation")
    calls = 0

    def operation(value: int) -> int:
        nonlocal calls
        calls += 1

        if calls < 3:
            raise RetryableError("temporary failure")

        return value * 2

    policy = ExecutionPolicy(
        max_attempts=3,
        retry_delay_seconds=0,
    )

    result = await execute(
        operation=operation,
        input_=10,
        context=context,
        policy=policy,
    )

    assert result.value == 20
    assert result.attempts == 3
    assert calls == 3


async def test_execute_stops_after_max_attempts():
    context = RunContext(operation_name="operation")
    calls = 0

    def operation(value: int) -> int:
        nonlocal calls
        calls += 1
        raise RetryableError("still failing")

    policy = ExecutionPolicy(
        max_attempts=3,
        retry_delay_seconds=0,
    )

    with pytest.raises(RetryableError):
        await execute(
            operation=operation,
            input_=10,
            context=context,
            policy=policy,
        )

    assert calls == 3


async def test_execute_does_not_retry_permanent_error():
    context = RunContext(operation_name="operation")
    calls = 0

    def operation(value: int) -> int:
        nonlocal calls
        calls += 1
        raise PermanentError("permanent failure")

    policy = ExecutionPolicy(
        max_attempts=5,
        retry_delay_seconds=0,
    )

    with pytest.raises(PermanentError):
        await execute(
            operation=operation,
            input_=10,
            context=context,
            policy=policy,
        )

    assert calls == 1


async def test_execute_runs_async_operation():
    context = RunContext(operation_name="operation")

    async def async_double(value: int) -> int:
        return value * 2

    result = await execute(
        operation=async_double,
        input_=21,
        context=context,
    )

    assert result.value == 42
    assert result.attempts == 1


async def test_execute_times_out_and_retries():
    sink = InMemoryEventSink()
    context = RunContext(run_id="run-timeout", operation_name="operation")
    calls = 0

    async def slow_then_fast(value: int) -> int:
        nonlocal calls
        calls += 1

        if calls == 1:
            await asyncio.sleep(1.0)

        return value * 2

    policy = ExecutionPolicy(
        max_attempts=2,
        timeout_seconds=0.05,
        retry_delay_seconds=0,
    )

    result = await execute(
        operation=slow_then_fast,
        input_=10,
        context=context,
        policy=policy,
        event_sink=sink,
    )

    assert result.value == 20
    assert result.attempts == 2
    assert calls == 2

    retrying = sink.events[1]
    assert retrying.name == "operation.retrying"
    assert retrying.attributes["error_type"] == "TimeoutError"


async def test_execute_emits_started_and_succeeded_events():
    sink = InMemoryEventSink()
    context = RunContext(run_id="run-123", operation_name="operation")

    result = await execute(
        operation=lambda value: value * 2,
        input_=5,
        context=context,
        event_sink=sink,
    )

    assert result.value == 10
    assert result.attempts == 1

    assert len(sink.events) == 2

    started = sink.events[0]
    assert started.name == "operation.started"
    assert started.run_id == "run-123"
    assert started.attributes["operation_name"] == "operation"

    succeeded = sink.events[1]
    assert succeeded.name == "operation.succeeded"
    assert succeeded.run_id == "run-123"
    assert succeeded.attributes["operation_name"] == "operation"
    assert succeeded.attributes["attempts"] == 1
    assert succeeded.attributes["duration_ms"] >= 0


async def test_execute_emits_retrying_event():
    sink = InMemoryEventSink()
    context = RunContext(run_id="run-123", operation_name="operation")

    attempts = 0

    def flaky_operation(value: int) -> int:
        nonlocal attempts
        attempts += 1

        if attempts == 1:
            raise RetryableError("temporary failure")

        return value * 2

    policy = ExecutionPolicy(
        max_attempts=2,
        retry_delay_seconds=0,
    )

    result = await execute(
        operation=flaky_operation,
        input_=5,
        context=context,
        policy=policy,
        event_sink=sink,
    )

    assert result.value == 10
    assert result.attempts == 2

    assert [event.name for event in sink.events] == [
        "operation.started",
        "operation.retrying",
        "operation.succeeded",
    ]

    retrying = sink.events[1]

    assert retrying.attributes["operation_name"] == "operation"
    assert retrying.attributes["attempt"] == 1
    assert retrying.attributes["next_attempt"] == 2
    assert retrying.attributes["error_type"] == "RetryableError"
    assert retrying.attributes["retry_delay_seconds"] == 0


async def test_execute_emits_failed_when_retries_exhausted():
    sink = InMemoryEventSink()
    context = RunContext(run_id="run-123", operation_name="operation")

    def always_fails(value: int) -> int:
        raise RetryableError("temporary failure")

    policy = ExecutionPolicy(
        max_attempts=2,
        retry_delay_seconds=0,
    )

    try:
        await execute(
            operation=always_fails,
            input_=5,
            context=context,
            policy=policy,
            event_sink=sink,
        )
    except RetryableError:
        pass

    assert [event.name for event in sink.events] == [
        "operation.started",
        "operation.retrying",
        "operation.failed",
    ]

    failed = sink.events[-1]

    assert failed.attributes["operation_name"] == "operation"
    assert failed.attributes["attempts"] == 2
    assert failed.attributes["error_type"] == "RetryableError"


# ---------------------------------------------------------------------------
# Foundation Hardening 0.1: sync-awaitable, indeterminate taxonomy, no-retry
# ---------------------------------------------------------------------------


async def test_sync_callable_returning_awaitable_is_awaited():
    """A plain function that returns a coroutine must have it awaited."""
    context = RunContext(operation_name="operation")

    async def inner(value: int) -> int:
        return value * 2

    def returns_coroutine(value: int):
        # Returns a coroutine without being a coroutine function itself.
        return inner(value)

    result = await execute(
        operation=returns_coroutine,
        input_=21,
        context=context,
    )

    assert result.value == 42
    assert not isinstance(result.value, asyncio.Future)


async def test_unexpected_failure_event_behavior_reversible():
    """Catch-all Exception on a reversible op emits operation.failed once."""
    sink = InMemoryEventSink()
    context = RunContext(run_id="run-err", operation_name="operation")

    def operation(value: str) -> str:
        raise KeyError("boom")

    with pytest.raises(PermanentError):
        await execute(
            operation=operation,
            input_="hello",
            context=context,
            event_sink=sink,
        )

    failed_events = [e for e in sink.events if e.name == "operation.failed"]
    indeterminate_events = [
        e for e in sink.events if e.name == "operation.indeterminate"
    ]
    assert len(failed_events) == 1
    assert len(indeterminate_events) == 0
    assert failed_events[0].attributes["error_type"] == "PermanentError"


async def test_unexpected_failure_on_irreversible_write_is_indeterminate():
    """Catch-all Exception on an irreversible write wraps as IndeterminateError
    and emits operation.indeterminate (not operation.failed, and not both)."""
    sink = InMemoryEventSink()
    context = RunContext(run_id="run-irr-err", operation_name="send")

    def operation(value: str) -> str:
        raise KeyError("parse error after submit")

    policy = ExecutionPolicy(
        max_attempts=1,
        irreversible_write=True,
    )

    with pytest.raises(IndeterminateError):
        await execute(
            operation=operation,
            input_="hello",
            context=context,
            policy=policy,
            event_sink=sink,
        )

    failed_events = [e for e in sink.events if e.name == "operation.failed"]
    indeterminate_events = [
        e for e in sink.events if e.name == "operation.indeterminate"
    ]
    assert len(failed_events) == 0
    assert len(indeterminate_events) == 1
    assert indeterminate_events[0].attributes["error_type"] == "IndeterminateError"


async def test_final_timeout_emits_failed_event():
    """A single-attempt reversible timeout emits operation.failed (not retrying)."""
    sink = InMemoryEventSink()
    context = RunContext(run_id="run-timeout-final", operation_name="operation")

    async def slow(value: int) -> int:
        await asyncio.sleep(1.0)
        return value

    policy = ExecutionPolicy(
        max_attempts=1,
        timeout_seconds=0.05,
    )

    from echo_v2.runtime.errors import TimeoutError as EchoTimeoutError

    with pytest.raises(EchoTimeoutError):
        await execute(
            operation=slow,
            input_=10,
            context=context,
            policy=policy,
            event_sink=sink,
        )

    assert [e.name for e in sink.events] == [
        "operation.started",
        "operation.failed",
    ]
    assert sink.events[-1].attributes["error_type"] == "TimeoutError"


async def test_irreversible_write_timeout_does_not_retry():
    """An irreversible-write timeout bypasses the retry loop immediately.

    Safety is structural: even with max_attempts=3, a timeout on attempt 1
    must NOT retry. Event stream is [started, indeterminate] (single-emission).
    """
    sink = InMemoryEventSink()
    context = RunContext(run_id="run-irr-timeout", operation_name="send")
    calls = 0

    async def always_slow(value: int) -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1.0)
        return value

    policy = ExecutionPolicy(
        max_attempts=3,
        timeout_seconds=0.05,
        irreversible_write=True,
    )

    from echo_v2.runtime.errors import TimeoutError as EchoTimeoutError

    with pytest.raises(EchoTimeoutError):
        await execute(
            operation=always_slow,
            input_=10,
            context=context,
            policy=policy,
            event_sink=sink,
        )

    assert calls == 1  # NOT 3 — no retry on irreversible timeout
    assert [e.name for e in sink.events] == [
        "operation.started",
        "operation.indeterminate",
    ]
    assert sink.events[-1].attributes["error_type"] == "TimeoutError"


async def test_retry_with_delay_sleeps_between_attempts():
    """Covers the retry_delay_seconds > 0 sleep path for both timeout
    and RetryableError retry arms."""

    # Timeout with retry delay (reversible, so it retries)
    calls = 0

    async def slow_then_fast(value: int) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0.3)
        return value * 2

    policy = ExecutionPolicy(
        max_attempts=2,
        timeout_seconds=0.05,
        retry_delay_seconds=0.01,
    )

    result = await execute(
        operation=slow_then_fast,
        input_=10,
        context=RunContext(operation_name="op"),
        policy=policy,
    )
    assert result.value == 20
    assert calls == 2

    # RetryableError with retry delay
    calls2 = 0

    def flaky_then_ok(value: int) -> int:
        nonlocal calls2
        calls2 += 1
        if calls2 < 2:
            raise RetryableError("temp")
        return value * 2

    policy2 = ExecutionPolicy(
        max_attempts=2,
        retry_delay_seconds=0.01,
    )

    result2 = await execute(
        operation=flaky_then_ok,
        input_=5,
        context=RunContext(operation_name="op"),
        policy=policy2,
    )
    assert result2.value == 10
    assert calls2 == 2


async def test_bare_execution_error_releases_idempotency_key():
    """A bare ExecutionError (base class, not a subclass) on an idempotent
    operation releases the key — covers the except ExecutionError fallback arm."""
    from echo_v2.runtime.errors import ExecutionError
    from echo_v2.runtime.idempotency import InMemoryIdempotencyStore

    store: InMemoryIdempotencyStore[int] = InMemoryIdempotencyStore()
    calls = 0

    def raises_bare(value: int) -> int:
        nonlocal calls
        calls += 1
        raise ExecutionError("bare")

    policy = ExecutionPolicy(max_attempts=1)

    with pytest.raises(ExecutionError):
        await execute(
            operation=raises_bare,
            input_=1,
            context=RunContext(operation_name="op"),
            policy=policy,
            idempotency_key="op:bare",
            idempotency_store=store,
        )
    assert calls == 1
    # Key should be released (no stored outcome).
    assert await store.get("op:bare") is None
