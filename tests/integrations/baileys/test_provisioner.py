from __future__ import annotations

import httpx
import pytest

from echo_v2.integrations.baileys.client import BaileysClient
from echo_v2.integrations.baileys.provisioner import BaileysProvisioner
from echo_v2.integrations.baileys.settings import BaileysSettings
from echo_v2.ports.whatsapp import (
    ConnectionConfig,
    ConnectionRef,
    ConnectionStatus,
    PairingOutcome,
    ProviderCredentials,
    WhatsAppProvisioner,
)


def _config() -> ConnectionConfig:
    return ConnectionConfig(webhook_url="https://unused", webhook_token="unused")


async def test_baileys_provisioner_satisfies_port_protocol():
    client = BaileysClient(
        BaileysSettings("http://connector", "secret"),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    201, json={"connection_id": "c1", "status": "provisioning"}
                )
            )
        ),
    )
    provisioner = BaileysProvisioner(client)
    assert isinstance(provisioner, WhatsAppProvisioner)
    await client.aclose()


async def test_create_uses_opaque_id_and_empty_credentials():
    client = BaileysClient(
        BaileysSettings("http://connector", "secret"),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(201, json={"connection_id": "c1"})
            )
        ),
    )
    created = await BaileysProvisioner(client).create_connection(_config())
    assert created.ref == ConnectionRef("baileys", "c1")
    assert created.credentials == ProviderCredentials(data=b"")
    await client.aclose()


async def test_status_and_qr_are_normalized():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"status": "connected", "provider_raw_status": "open"})
        return httpx.Response(200, json={"outcome": "qr_ready", "image_base64": "abc"})

    client = BaileysClient(
        BaileysSettings("http://connector", "secret"),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    provisioner = BaileysProvisioner(client)
    ref = ConnectionRef("baileys", "c1")
    snapshot = await provisioner.get_status(ref)
    result = await provisioner.get_pairing_qr(ref)
    assert snapshot.status is ConnectionStatus.CONNECTED
    assert snapshot.provider_raw_status == "open"
    assert result.outcome is PairingOutcome.QR_READY
    assert result.qr is not None and result.qr.image_base64 == "abc"
    await client.aclose()


@pytest.mark.parametrize("raw", ["not-a-status", None, 123])
async def test_unknown_status_maps_to_unknown(raw):
    client = BaileysClient(
        BaileysSettings("http://connector", "secret"),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"status": raw})
            )
        ),
    )
    snapshot = await BaileysProvisioner(client).get_status(
        ConnectionRef("baileys", "c1")
    )
    assert snapshot.status is ConnectionStatus.UNKNOWN
    await client.aclose()


async def test_pairing_outcomes_are_normalized():
    responses = iter([
        {"outcome": "already_authorized"},
        {"outcome": "timeout", "message": "not yet"},
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    client = BaileysClient(
        BaileysSettings("http://connector", "secret"),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    provisioner = BaileysProvisioner(client)
    ref = ConnectionRef("baileys", "c1")
    assert (await provisioner.get_pairing_qr(ref)).outcome is PairingOutcome.ALREADY_AUTHORIZED
    assert (await provisioner.get_pairing_qr(ref)).outcome is PairingOutcome.TIMEOUT
    await client.aclose()


async def test_configure_is_a_no_op():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    client = BaileysClient(
        BaileysSettings("http://connector", "secret"),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await BaileysProvisioner(client).configure_connection(
        ConnectionRef("baileys", "c1"), _config()
    )
    assert not called
    await client.aclose()
