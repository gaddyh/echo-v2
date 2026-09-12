"""Privacy sanitizers for Sprint 2 operational tracing.

Each traced service method gets a ``safe_*`` pair:
- ``safe_*_inputs`` — strips ``self`` and PII, keeps safe correlation fields.
- ``safe_*_output`` — strips PII from the result, keeps safe outcome fields.

All user/chat IDs are HMAC-hashed via :func:`echo_v2.observability.privacy.correlation_id`.
No phone numbers, chat IDs, message text, headers, or API keys appear in
sanitized metadata.
"""

from __future__ import annotations

from typing import Any

from echo_v2.domain.feedback import HandlingOutcome
from echo_v2.observability.privacy import correlation_id

__all__ = [
    "safe_action_inputs",
    "safe_action_output",
    "safe_bot_send_inputs",
    "safe_bot_send_output",
    "safe_dialog360_http_inputs",
    "safe_dialog360_http_output",
    "safe_feedback_handle_inputs",
    "safe_feedback_handle_output",
    "safe_green_http_inputs",
    "safe_green_http_output",
    "safe_scheduling_execute_inputs",
    "safe_scheduling_execute_output",
    "safe_webhook_inputs",
    "safe_webhook_output",
]


def _hash_if_present(value: str | None) -> str | None:
    if value is None:
        return None
    return correlation_id(value)


def safe_action_inputs(inputs: dict) -> dict:
    """Sanitize WaitingForMeActionService method inputs.

    Strips ``self`` and all raw IDs. Keeps hashed ``user_id``, ``active_id``,
    ``target_version``, and the action type (method name).
    """
    user_id = inputs.get("user_id")
    active_id = inputs.get("active_id")
    target_version = inputs.get("target_version")
    sanitized: dict[str, Any] = {
        "target_version": target_version,
    }
    if user_id is not None:
        sanitized["user_id_hash"] = _hash_if_present(user_id)
    if active_id is not None:
        sanitized["active_id_hash"] = _hash_if_present(active_id)
    return sanitized


def safe_action_output(output: HandlingOutcome) -> dict:
    """Sanitize action method output — just the outcome name."""
    return {"outcome": output.value if hasattr(output, "value") else str(output)}


def safe_feedback_handle_inputs(inputs: dict) -> dict:
    """Sanitize FeedbackHandler.handle inputs.

    Strips ``self`` and the full ``BotEvent``. The event contains phone
    numbers and message text — never let it into trace metadata.
    """
    return {"event_type": "bot_event"}


def safe_feedback_handle_output(output: bool) -> dict:
    """Sanitize FeedbackHandler.handle output."""
    return {"handled": output}


def safe_scheduling_execute_inputs(inputs: dict) -> dict:
    """Sanitize SchedulingService.execute inputs.

    Strips ``self`` and the full ``ScheduledAction`` (which contains
    chat_id, message text, phone numbers in payload). Keeps only the
    action type and hashed action ID.
    """
    action = inputs.get("action")
    if action is None:
        return {}
    return {
        "action_type": action.type.value if hasattr(action.type, "value") else str(action.type),
        "action_id_hash": _hash_if_present(str(action.id)),
        "user_id_hash": _hash_if_present(action.user_id),
    }


def safe_scheduling_execute_output(output: str) -> dict:
    """Sanitize SchedulingService.execute output — the provider message ID."""
    return {"result": output}


def safe_bot_send_inputs(inputs: dict) -> dict:
    """Sanitize _execute_bot_send inputs.

    Strips ``self`` and the full ``ScheduledAction``. Keeps action type
    and hashed IDs. The payload contains chat_id (phone) and message
    text — never include those.
    """
    action = inputs.get("action")
    if action is None:
        return {}
    kind = action.payload.get("kind") if hasattr(action, "payload") else None
    sanitized: dict[str, Any] = {
        "action_id_hash": _hash_if_present(str(action.id)),
        "user_id_hash": _hash_if_present(action.user_id),
    }
    if kind:
        sanitized["kind"] = kind
    return sanitized


def safe_bot_send_output(output: str) -> dict:
    """Sanitize _execute_bot_send output."""
    return {"result": output}


def safe_dialog360_http_inputs(inputs: dict) -> dict:
    """Sanitize Dialog360._post_json inputs.

    Strips ``self``, URL, headers, and payload (which contains phone
    numbers and message text). Keeps only ``provider``, ``operation``,
    and ``is_write``.
    """
    operation = inputs.get("operation")
    return {
        "provider": "dialog360",
        "operation": operation,
        "is_write": True,
    }


def safe_dialog360_http_output(output: dict) -> dict:
    """Sanitize Dialog360._post_json output — just whether it succeeded."""
    # The output is the parsed JSON body; we only report that it returned.
    return {"status": "ok"}


def safe_green_http_inputs(inputs: dict) -> dict:
    """Sanitize Green._request_json inputs.

    Strips ``self``, URL, method, headers, json_body, and connection_id.
    Keeps only ``provider``, ``operation``, ``is_write``, and a hashed
    ``connection_id``.
    """
    operation = inputs.get("operation")
    connection_id = inputs.get("connection_id")
    is_write = inputs.get("is_write")
    sanitized: dict[str, Any] = {
        "provider": "green",
        "operation": operation,
        "is_write": bool(is_write),
    }
    if connection_id is not None:
        sanitized["connection_id_hash"] = _hash_if_present(connection_id)
    return sanitized


def safe_green_http_output(output: dict) -> dict:
    """Sanitize Green._request_json output — just whether it returned."""
    return {"status": "ok"}


def safe_webhook_inputs(inputs: dict) -> dict:
    """Sanitize webhook entry-point inputs.

    Strips ``request``, ``authorization`` header, and the raw payload.
    Keeps only ``provider`` and ``event_type`` (if parseable).
    """
    return {"provider": "webhook"}


def safe_webhook_output(output: dict) -> dict:
    """Sanitize webhook output — just the status string."""
    if isinstance(output, dict):
        return {"status": output.get("status", "unknown")}
    return {"status": str(output)}
