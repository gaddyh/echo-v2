"""Tests for Green status mapping and subscription translation."""

from __future__ import annotations

import pytest

from echo_v2.integrations.green.models import (
    GREEN_STATE_AUTHORIZED,
    GREEN_STATE_BLOCKED,
    GREEN_STATE_NOT_AUTHORIZED,
    GREEN_STATE_SLEEP_MODE,
    GREEN_STATE_STARTING,
    GREEN_STATE_SUSPENDED,
    GREEN_STATE_YELLOW_CARD,
    map_state,
    subscription_to_green_fields,
)
from echo_v2.ports.whatsapp import ConnectionStatus, WhatsAppEventSubscription


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, ConnectionStatus.PROVISIONING),
        ("", ConnectionStatus.PROVISIONING),
        (GREEN_STATE_STARTING, ConnectionStatus.CONNECTING),
        (GREEN_STATE_NOT_AUTHORIZED, ConnectionStatus.PAIRING_REQUIRED),
        (GREEN_STATE_AUTHORIZED, ConnectionStatus.CONNECTED),
        (GREEN_STATE_SLEEP_MODE, ConnectionStatus.DEGRADED),
        (GREEN_STATE_BLOCKED, ConnectionStatus.BLOCKED),
        (GREEN_STATE_SUSPENDED, ConnectionStatus.SUSPENDED),
        (GREEN_STATE_YELLOW_CARD, ConnectionStatus.SUSPENDED),
        ("brandNewGreenState", ConnectionStatus.UNKNOWN),
    ],
)
def test_map_state(raw, expected):
    assert map_state(raw) is expected


def test_subscription_to_green_fields_defaults_all_yes():
    fields = subscription_to_green_fields(WhatsAppEventSubscription())
    assert fields == {
        "incomingWebhook": "yes",
        "outgoingMessageWebhook": "yes",
        "outgoingAPIMessageWebhook": "yes",
        "outgoingWebhook": "yes",
        "stateWebhook": "yes",
    }


def test_subscription_to_green_fields_respects_flags():
    sub = WhatsAppEventSubscription(
        incoming_messages=False,
        outgoing_phone_messages=False,
        outgoing_api_messages=False,
        message_statuses=False,
        connection_state=False,
    )
    fields = subscription_to_green_fields(sub)
    assert all(v == "no" for v in fields.values())


def test_subscription_to_green_fields_mixed():
    sub = WhatsAppEventSubscription(
        incoming_messages=True,
        message_statuses=False,
    )
    fields = subscription_to_green_fields(sub)
    assert fields["incomingWebhook"] == "yes"
    assert fields["outgoingWebhook"] == "no"
