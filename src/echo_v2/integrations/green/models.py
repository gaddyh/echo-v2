"""Green-specific model helpers: status mapping and config translation.

This is the one place where Green's vocabulary (``stateInstance`` strings,
``incomingWebhook`` switches) is translated to/from Echo's provider-neutral
types. Everything else in the integration talks in neutral terms.
"""

from __future__ import annotations

from echo_v2.ports.whatsapp import (
    ConnectionStatus,
    WhatsAppEventSubscription,
)

__all__ = [
    "GREEN_STATE_AUTHORIZED",
    "GREEN_STATE_BLOCKED",
    "GREEN_STATE_NOT_AUTHORIZED",
    "GREEN_STATE_SLEEP_MODE",
    "GREEN_STATE_STARTING",
    "GREEN_STATE_SUSPENDED",
    "GREEN_STATE_YELLOW_CARD",
    "map_state",
    "subscription_to_green_fields",
]


# Green ``stateInstance`` raw values (current docs).
GREEN_STATE_NOT_AUTHORIZED = "notAuthorized"
GREEN_STATE_AUTHORIZED = "authorized"
GREEN_STATE_BLOCKED = "blocked"
GREEN_STATE_SLEEP_MODE = "sleepMode"
GREEN_STATE_STARTING = "starting"
GREEN_STATE_SUSPENDED = "suspended"
# Legacy value replaced by ``suspended`` in current Green docs; map both.
GREEN_STATE_YELLOW_CARD = "yellowCard"


def map_state(raw: str | None) -> ConnectionStatus:
    """Map a Green ``stateInstance`` string to a neutral :class:`ConnectionStatus`.

    ``None`` (Green returns null while an instance is being created) maps to
    ``PROVISIONING``. Any unrecognized value maps to ``UNKNOWN`` so a new
    Green status is never silently misclassified.
    """

    if raw is None or raw == "":
        return ConnectionStatus.PROVISIONING
    if raw == GREEN_STATE_STARTING:
        return ConnectionStatus.CONNECTING
    if raw == GREEN_STATE_NOT_AUTHORIZED:
        return ConnectionStatus.PAIRING_REQUIRED
    if raw == GREEN_STATE_AUTHORIZED:
        return ConnectionStatus.CONNECTED
    if raw == GREEN_STATE_SLEEP_MODE:
        return ConnectionStatus.DEGRADED
    if raw == GREEN_STATE_BLOCKED:
        return ConnectionStatus.BLOCKED
    if raw in (GREEN_STATE_SUSPENDED, GREEN_STATE_YELLOW_CARD):
        return ConnectionStatus.SUSPENDED
    return ConnectionStatus.UNKNOWN


def subscription_to_green_fields(
    subscription: WhatsAppEventSubscription,
) -> dict[str, str]:
    """Translate a :class:`WhatsAppEventSubscription` to Green's webhook
    switch fields (the ``yes``/``no`` strings Green expects)."""

    def yn(flag: bool) -> str:
        return "yes" if flag else "no"

    return {
        "incomingWebhook": yn(subscription.incoming_messages),
        "outgoingMessageWebhook": yn(subscription.outgoing_phone_messages),
        "outgoingAPIMessageWebhook": yn(subscription.outgoing_api_messages),
        "outgoingWebhook": yn(subscription.message_statuses),
        "stateWebhook": yn(subscription.connection_state),
    }
