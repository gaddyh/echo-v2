import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Generic, TypeVar

from echo_v2.runtime.context import RunContext
from echo_v2.runtime.errors import (
    ApplicationError,
    ExecutionError,
    IndeterminateError,
    PermanentError,
    RetryableError,
    TimeoutError,
)
from echo_v2.runtime.events import NO_OP_SINK, EventSink, RuntimeEvent
from echo_v2.runtime.idempotency import (
    IdempotencyStore,
    IndeterminateOutcome,
    PermanentFailureOutcome,
    ReserveStatus,
    StoredOutcome,
    SuccessOutcome,
)
from echo_v2.runtime.policy import NO_RETRY, ExecutionPolicy

TInput = TypeVar("TInput")
TOutput = TypeVar("TOutput")

Operation = Callable[[TInput], TOutput | Awaitable[TOutput]]


@dataclass(frozen=True)
class ExecutionResult(Generic[TOutput]):
    value: TOutput
    duration_ms: float
    attempts: int


def emit_failed(
    *,
    event_sink: EventSink,
    context: RunContext,
    error: Exception,
    attempts: int,
    duration_ms: float,
    idempotency_key: str | None = None,
) -> None:
    attributes: dict = {
        "operation_name": context.operation_name,
        "attempts": attempts,
        "duration_ms": duration_ms,
        "error_type": type(error).__name__,
    }
    if idempotency_key is not None:
        attributes["idempotency_key"] = idempotency_key

    event_sink.emit(
        RuntimeEvent(
            name="operation.failed",
            run_id=context.run_id,
            attributes=attributes,
        )
    )


async def _run_operation(
    operation: Operation[TInput, TOutput],
    input_: TInput,
) -> TOutput:
    if inspect.iscoroutinefunction(operation):
        return await operation(input_)

    result = await asyncio.to_thread(operation, input_)
    if inspect.isawaitable(result):
        return await result
    return result


async def _run_with_retries(
    operation: Operation[TInput, TOutput],
    input_: TInput,
    context: RunContext,
    policy: ExecutionPolicy,
    event_sink: EventSink,
    idempotency_key: str | None = None,
) -> ExecutionResult[TOutput]:
    """Run ``operation`` with retry, timeout, and event emission.

    Emits ``operation.started`` at the top (so this helper must only be
    called on a path where actual execution is about to begin), then
    ``operation.retrying`` / ``operation.succeeded`` / ``operation.failed``
    per the existing contract.
    """
    start = perf_counter()

    started_attributes: dict = {
        "operation_name": context.operation_name,
    }
    if idempotency_key is not None:
        started_attributes["idempotency_key"] = idempotency_key

    event_sink.emit(
        RuntimeEvent(
            name="operation.started",
            run_id=context.run_id,
            attributes=started_attributes,
        )
    )

    for attempt in range(1, policy.max_attempts + 1):
        try:
            value = await asyncio.wait_for(
                _run_operation(operation, input_),
                timeout=policy.timeout_seconds,
            )

            duration_ms = (perf_counter() - start) * 1000

            succeeded_attributes: dict = {
                "operation_name": context.operation_name,
                "attempts": attempt,
                "duration_ms": duration_ms,
            }
            if idempotency_key is not None:
                succeeded_attributes["idempotency_key"] = idempotency_key

            event_sink.emit(
                RuntimeEvent(
                    name="operation.succeeded",
                    run_id=context.run_id,
                    attributes=succeeded_attributes,
                )
            )

            return ExecutionResult(
                value=value,
                duration_ms=duration_ms,
                attempts=attempt,
            )

        except asyncio.TimeoutError as exc:
            wrapped = TimeoutError(f"Operation timed out during run {context.run_id}")

            # An irreversible write that times out may have already produced
            # its side effect. Do NOT retry — propagate so execute() can store
            # INDETERMINATE. Safety is structural, not dependent on
            # max_attempts=1.
            if policy.irreversible_write:
                duration_ms = (perf_counter() - start) * 1000
                _emit_indeterminate(
                    event_sink=event_sink,
                    context=context,
                    error=wrapped,
                    duration_ms=duration_ms,
                    idempotency_key=idempotency_key,
                )
                raise wrapped from exc

            if attempt >= policy.max_attempts:
                duration_ms = (perf_counter() - start) * 1000

                emit_failed(
                    event_sink=event_sink,
                    context=context,
                    error=wrapped,
                    attempts=attempt,
                    duration_ms=duration_ms,
                    idempotency_key=idempotency_key,
                )
                raise wrapped from exc

            _emit_retrying(
                event_sink=event_sink,
                context=context,
                attempt=attempt,
                error=wrapped,
                policy=policy,
                idempotency_key=idempotency_key,
            )

            if policy.retry_delay_seconds > 0:
                await asyncio.sleep(policy.retry_delay_seconds)

        except RetryableError as exc:
            if attempt >= policy.max_attempts:
                duration_ms = (perf_counter() - start) * 1000

                emit_failed(
                    event_sink=event_sink,
                    context=context,
                    error=exc,
                    attempts=attempt,
                    duration_ms=duration_ms,
                    idempotency_key=idempotency_key,
                )
                raise

            _emit_retrying(
                event_sink=event_sink,
                context=context,
                attempt=attempt,
                error=exc,
                policy=policy,
                idempotency_key=idempotency_key,
            )

            if policy.retry_delay_seconds > 0:
                await asyncio.sleep(policy.retry_delay_seconds)

        except ApplicationError as exc:
            duration_ms = (perf_counter() - start) * 1000

            emit_failed(
                event_sink=event_sink,
                context=context,
                error=exc,
                attempts=attempt,
                duration_ms=duration_ms,
                idempotency_key=idempotency_key,
            )
            raise

        except IndeterminateError as exc:
            # Explicitly raised by an integration that knows the outcome is
            # ambiguous. Not retryable. Emit indeterminate (not failed) and
            # propagate so execute() can store INDETERMINATE.
            duration_ms = (perf_counter() - start) * 1000

            _emit_indeterminate(
                event_sink=event_sink,
                context=context,
                error=exc,
                duration_ms=duration_ms,
                idempotency_key=idempotency_key,
            )
            raise

        except ExecutionError as exc:
            duration_ms = (perf_counter() - start) * 1000

            emit_failed(
                event_sink=event_sink,
                context=context,
                error=exc,
                attempts=attempt,
                duration_ms=duration_ms,
                idempotency_key=idempotency_key,
            )
            raise

        except Exception as exc:
            # For irreversible writes, an unexpected error after the request
            # was submitted (e.g. KeyError parsing an unfamiliar response
            # shape) means the side effect may have happened. Conservatively
            # treat as indeterminate rather than claiming "definitely did not
            # send." For reads/compute, wrap as PermanentError (current
            # behavior).
            if policy.irreversible_write:
                wrapped = IndeterminateError(
                    f"Unexpected failure during irreversible run {context.run_id}"
                )
                duration_ms = (perf_counter() - start) * 1000

                _emit_indeterminate(
                    event_sink=event_sink,
                    context=context,
                    error=wrapped,
                    duration_ms=duration_ms,
                    idempotency_key=idempotency_key,
                )
            else:
                wrapped = PermanentError(
                    f"Unexpected failure during run {context.run_id}"
                )
                duration_ms = (perf_counter() - start) * 1000

                emit_failed(
                    event_sink=event_sink,
                    context=context,
                    error=wrapped,
                    attempts=attempt,
                    duration_ms=duration_ms,
                    idempotency_key=idempotency_key,
                )

            raise wrapped from exc

    raise RuntimeError("unreachable")  # pragma: no cover


def _emit_retrying(
    *,
    event_sink: EventSink,
    context: RunContext,
    attempt: int,
    error: Exception,
    policy: ExecutionPolicy,
    idempotency_key: str | None,
) -> None:
    attributes: dict = {
        "operation_name": context.operation_name,
        "attempt": attempt,
        "next_attempt": attempt + 1,
        "error_type": type(error).__name__,
        "retry_delay_seconds": policy.retry_delay_seconds,
    }
    if idempotency_key is not None:
        attributes["idempotency_key"] = idempotency_key

    event_sink.emit(
        RuntimeEvent(
            name="operation.retrying",
            run_id=context.run_id,
            attributes=attributes,
        )
    )


def _emit_idempotent(
    *,
    event_sink: EventSink,
    context: RunContext,
    name: str,
    idempotency_key: str,
) -> None:
    event_sink.emit(
        RuntimeEvent(
            name=name,
            run_id=context.run_id,
            attributes={
                "operation_name": context.operation_name,
                "idempotency_key": idempotency_key,
            },
        )
    )


def _emit_indeterminate(
    *,
    event_sink: EventSink,
    context: RunContext,
    error: BaseException,
    duration_ms: float,
    idempotency_key: str | None = None,
) -> None:
    """Emit ``operation.indeterminate`` for an unknown-outcome failure.

    Mutually exclusive with ``emit_failed``: an operation emits exactly one
    terminal event (succeeded / failed / indeterminate). Used when the side
    effect may have happened (timeout, cancellation, or unexpected error on
    an irreversible write).
    """
    attributes: dict = {
        "operation_name": context.operation_name,
        "duration_ms": duration_ms,
        "error_type": type(error).__name__,
    }
    if idempotency_key is not None:
        attributes["idempotency_key"] = idempotency_key

    event_sink.emit(
        RuntimeEvent(
            name="operation.indeterminate",
            run_id=context.run_id,
            attributes=attributes,
        )
    )


def _replay_failure(
    outcome: PermanentFailureOutcome,
    context: RunContext,
) -> PermanentError:
    return PermanentError(
        f"Idempotent operation {context.operation_name!r} "
        f"previously failed permanently: {outcome.error_message}"
    )


def _replay_indeterminate(
    outcome: IndeterminateOutcome,
    context: RunContext,
) -> IndeterminateError:
    return IndeterminateError(
        f"Idempotent operation {context.operation_name!r} "
        f"previously ended with unknown outcome: {outcome.error_message}"
    )


async def execute(
    operation: Operation[TInput, TOutput],
    input_: TInput,
    context: RunContext,
    policy: ExecutionPolicy = NO_RETRY,
    event_sink: EventSink = NO_OP_SINK,
    idempotency_key: str | None = None,
    idempotency_store: IdempotencyStore[TOutput] | None = None,
) -> ExecutionResult[TOutput]:
    if (idempotency_key is None) != (idempotency_store is None):
        raise ValueError(
            "idempotency_key and idempotency_store must be provided together"
        )

    if idempotency_key is None or idempotency_store is None:
        return await _run_with_retries(
            operation=operation,
            input_=input_,
            context=context,
            policy=policy,
            event_sink=event_sink,
        )

    store = idempotency_store
    key = idempotency_key

    # 1. Fast path: a terminal outcome is already cached.
    cached = await store.get(key)
    if cached is not None:
        return await _handle_outcome(
            cached,
            context,
            event_sink,
            key,
        )

    # 2. Try to claim the key.
    status = await store.reserve(key)

    if status == ReserveStatus.COMPLETED:
        outcome = await store.get(key)
        if outcome is not None:
            return await _handle_outcome(
                outcome,
                context,
                event_sink,
                key,
            )
        # Rare race: outcome vanished. Fall through to re-reserve.  # pragma: no cover
        status = await store.reserve(key)  # pragma: no cover

    if status == ReserveStatus.IN_PROGRESS:
        _emit_idempotent(
            event_sink=event_sink,
            context=context,
            name="operation.idempotent.waiting",
            idempotency_key=key,
        )
        outcome = await store.wait_for_completion(key)
        return await _handle_outcome(
            outcome,
            context,
            event_sink,
            key,
        )

    # 3. ACQUIRED: this caller owns execution.
    # _run_with_retries emits the terminal event (succeeded/failed/
    # indeterminate) for all non-cancellation cases. The owner block only
    # persists the outcome; it does NOT emit a second terminal event except
    # for cancellation (which bypasses _run_with_retries' exception handling).
    # All store-mutating cleanup uses asyncio.shield to protect against a
    # second cancellation interrupting the state transition mid-flight.
    try:
        result = await _run_with_retries(
            operation=operation,
            input_=input_,
            context=context,
            policy=policy,
            event_sink=event_sink,
            idempotency_key=key,
        )
    except asyncio.CancelledError:
        # Cancellation tells us the caller stopped waiting, NOT that the side
        # effect didn't happen (especially under to_thread, which can't stop
        # the underlying thread). For irreversible writes, conservatively
        # treat as indeterminate.
        if policy.irreversible_write:
            await asyncio.shield(
                store.put_indeterminate(
                    key,
                    IndeterminateOutcome(
                        error_type="CancelledError",
                        error_message=f"Operation cancelled during run {context.run_id}",
                    ),
                )
            )
            _emit_indeterminate(
                event_sink=event_sink,
                context=context,
                error=asyncio.CancelledError(),
                duration_ms=0.0,
                idempotency_key=key,
            )
        else:
            await asyncio.shield(store.release(key))
        raise
    except PermanentError as exc:
        await asyncio.shield(
            store.put_failure(
                key,
                PermanentFailureOutcome(
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                ),
            )
        )
        raise
    except IndeterminateError as exc:
        await asyncio.shield(
            store.put_indeterminate(
                key,
                IndeterminateOutcome(
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                ),
            )
        )
        raise
    except TimeoutError as exc:
        if policy.irreversible_write:
            await asyncio.shield(
                store.put_indeterminate(
                    key,
                    IndeterminateOutcome(
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    ),
                )
            )
        else:
            await asyncio.shield(store.release(key))
        raise
    except RetryableError:
        # Unambiguous "did not complete" (e.g. 429, connection refused before
        # send). Release regardless of irreversible_write — do NOT over-block
        # idempotency keys for trivially-retryable conditions.
        await asyncio.shield(store.release(key))
        raise
    except ExecutionError:
        # Base-class fallback (should not normally be raised directly).
        await asyncio.shield(store.release(key))
        raise
    except Exception as exc:  # pragma: no cover
        # Already wrapped by _run_with_retries as PermanentError or
        # IndeterminateError. This arm should not normally fire from the owner
        # path; keep as a defensive put_failure if it ever does.
        await asyncio.shield(
            store.put_failure(
                key,
                PermanentFailureOutcome(
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                ),
            )
        )
        raise  # pragma: no cover

    await asyncio.shield(
        store.put_success(
            key,
            SuccessOutcome(
                value=result.value,
                attempts=result.attempts,
                duration_ms=result.duration_ms,
            ),
        )
    )
    return result


async def _handle_outcome(
    outcome: StoredOutcome[TOutput],
    context: RunContext,
    event_sink: EventSink,
    idempotency_key: str,
) -> ExecutionResult[TOutput]:
    if isinstance(outcome, SuccessOutcome):
        _emit_idempotent(
            event_sink=event_sink,
            context=context,
            name="operation.idempotent.hit",
            idempotency_key=idempotency_key,
        )
        return ExecutionResult(
            value=outcome.value,
            duration_ms=outcome.duration_ms,
            attempts=outcome.attempts,
        )

    if isinstance(outcome, IndeterminateOutcome):
        _emit_idempotent(
            event_sink=event_sink,
            context=context,
            name="operation.idempotent.indeterminate",
            idempotency_key=idempotency_key,
        )
        raise _replay_indeterminate(outcome, context)

    # PermanentFailureOutcome
    _emit_idempotent(
        event_sink=event_sink,
        context=context,
        name="operation.idempotent.replayed",
        idempotency_key=idempotency_key,
    )
    raise _replay_failure(outcome, context)
