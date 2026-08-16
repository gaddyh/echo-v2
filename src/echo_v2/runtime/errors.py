class ApplicationError(Exception):
    """Base exception for expected application errors."""


class ExecutionError(Exception):
    """Base class for runtime execution failures."""


class RetryableError(ExecutionError):
    """A transient failure that may succeed on a later attempt."""


class TimeoutError(RetryableError):
    """An operation exceeded its configured timeout. Retryable."""


class PermanentError(ExecutionError):
    """A failure that should not be retried."""


class IndeterminateError(ExecutionError):
    """An operation whose outcome is unknown (e.g. timed out mid-flight).

    Not retryable, not permanent: the side effect may or may not have
    happened. For idempotent irreversible writes, this is persisted as an
    ``IndeterminateOutcome`` so the same key cannot re-run until reconciled.
    """
