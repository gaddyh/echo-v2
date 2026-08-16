import pytest

from echo_v2.runtime.policy import EXTERNAL_WRITE, ExecutionPolicy


def test_policy_rejects_max_attempts_zero():
    with pytest.raises(ValueError, match="max_attempts"):
        ExecutionPolicy(max_attempts=0)


def test_policy_rejects_negative_timeout():
    with pytest.raises(ValueError, match="timeout_seconds"):
        ExecutionPolicy(timeout_seconds=-5)


def test_policy_rejects_negative_retry_delay():
    with pytest.raises(ValueError, match="retry_delay_seconds"):
        ExecutionPolicy(retry_delay_seconds=-1)


def test_external_write_is_irreversible():
    assert EXTERNAL_WRITE.irreversible_write is True
