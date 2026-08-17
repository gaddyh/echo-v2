from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionPolicy:
    max_attempts: int = 1
    timeout_seconds: float | None = None
    retry_delay_seconds: float = 0.0
    irreversible_write: bool = False

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError(
                f"timeout_seconds must be > 0 or None, got {self.timeout_seconds}"
            )
        if self.retry_delay_seconds < 0:
            raise ValueError(
                f"retry_delay_seconds must be >= 0, got {self.retry_delay_seconds}"
            )


NO_RETRY = ExecutionPolicy(
    max_attempts=1,
)

LOCAL_COMPUTE = ExecutionPolicy(
    max_attempts=1,
    timeout_seconds=5.0,
)

EXTERNAL_READ = ExecutionPolicy(
    max_attempts=3,
    timeout_seconds=10.0,
    retry_delay_seconds=0.5,
)

EXTERNAL_WRITE = ExecutionPolicy(
    max_attempts=1,
    timeout_seconds=10.0,
    irreversible_write=True,
)
