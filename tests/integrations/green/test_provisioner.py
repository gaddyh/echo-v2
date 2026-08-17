"""Tests for GreenProvisioner against a fake GreenClient + fake resolver."""

from __future__ import annotations

import pytest

from echo_v2.integrations.green.provisioner import GreenProvisioner, _qr_result
from echo_v2.ports.whatsapp import (
    ConnectionConfig,
    ConnectionRef,
    ConnectionStatus,
    ConnectionStatusSnapshot,
    CredentialResolver,
    PairingOutcome,
    PairingQr,
    ProviderCredentials,
    WhatsAppEventSubscription,
    WhatsAppProvisioner,
)
from echo_v2.runtime.errors import PermanentError


class FakeGreenClient:
    """Records calls and returns canned responses."""

    def __init__(
        self,
        *,
        create_response: dict | None = None,
        state_response: str | None = "authorized",
        qr_response: dict | None = None,
    ) -> None:
        self.create_response = create_response or {
            "idInstance": 123,
            "apiTokenInstance": "api-tok",
        }
        self.state_response = state_response
        self.qr_response = qr_response or {"type": "qrCode", "message": "b64png"}
        self.calls: list[tuple[str, tuple, dict]] = []

    async def create_instance(self, payload):
        self.calls.append(("create_instance", (), dict(payload)))
        return self.create_response

    async def set_settings(self, id_instance, api_token, settings):
        self.calls.append(("set_settings", (id_instance, api_token), dict(settings)))

    async def get_state_instance(self, id_instance, api_token):
        self.calls.append(("get_state", (id_instance, api_token), {}))
        return self.state_response

    async def get_qr_ws(self, id_instance, api_token, *, timeout=100.0):
        self.calls.append(("get_qr", (id_instance, api_token), {"timeout": timeout}))
        return self.qr_response

    async def logout(self, id_instance, api_token):
        self.calls.append(("logout", (id_instance, api_token), {}))

    async def delete_instance(self, instance_id):
        self.calls.append(("delete_instance", (instance_id,), {}))


class FakeResolver:
    def __init__(self, credentials: ProviderCredentials | None) -> None:
        self._credentials = credentials

    async def get_credentials(self, ref: ConnectionRef) -> ProviderCredentials | None:
        return self._credentials


def _config() -> ConnectionConfig:
    return ConnectionConfig(
        webhook_url="https://echo/hook",
        webhook_token="tok",
        subscriptions=WhatsAppEventSubscription(),
    )


def test_green_provisioner_satisfies_port_protocol():
    prov = GreenProvisioner.__new__(GreenProvisioner)
    assert isinstance(prov, WhatsAppProvisioner)


async def test_create_connection_returns_ref_and_credentials_with_only_token():
    client = FakeGreenClient()
    prov = GreenProvisioner(client, FakeResolver(None))
    created = await prov.create_connection(_config())
    assert created.ref == ConnectionRef("green", "123")
    # Credentials carry ONLY the api token, not idInstance.
    assert created.credentials.data == b"api-tok"
    assert b"123" not in created.credentials.data


async def test_create_connection_passes_webhook_url_and_token_through():
    client = FakeGreenClient()
    prov = GreenProvisioner(client, FakeResolver(None))
    await prov.create_connection(_config())
    name, _, payload = client.calls[0]
    assert name == "create_instance"
    assert payload["webhookUrl"] == "https://echo/hook"
    assert payload["webhookUrlToken"] == "tok"
    assert payload["incomingWebhook"] == "yes"
    assert payload["stateWebhook"] == "yes"


async def test_create_connection_raises_on_missing_response_field():
    client = FakeGreenClient(create_response={"idInstance": 123})  # no apiTokenInstance
    prov = GreenProvisioner(client, FakeResolver(None))
    with pytest.raises(PermanentError):
        await prov.create_connection(_config())


async def test_get_status_maps_state():
    client = FakeGreenClient(state_response="sleepMode")
    prov = GreenProvisioner(client, FakeResolver(ProviderCredentials(b"api-tok")))
    snapshot = await prov.get_status(ConnectionRef("green", "123"))
    assert snapshot.status is ConnectionStatus.DEGRADED
    assert snapshot.provider_raw_status == "sleepMode"
    assert isinstance(snapshot, ConnectionStatusSnapshot)


async def test_get_status_unknown_state_maps_to_unknown():
    client = FakeGreenClient(state_response="brandNew")
    prov = GreenProvisioner(client, FakeResolver(ProviderCredentials(b"api-tok")))
    snapshot = await prov.get_status(ConnectionRef("green", "123"))
    assert snapshot.status is ConnectionStatus.UNKNOWN


async def test_configure_connection_calls_set_settings_with_webhook_fields():
    client = FakeGreenClient()
    prov = GreenProvisioner(client, FakeResolver(ProviderCredentials(b"api-tok")))
    await prov.configure_connection(ConnectionRef("green", "123"), _config())
    name, (id_instance, api_token), settings = client.calls[0]
    assert name == "set_settings"
    assert id_instance == "123"
    assert api_token == "api-tok"
    assert settings["webhookUrl"] == "https://echo/hook"
    assert settings["webhookUrlToken"] == "tok"
    assert settings["incomingWebhook"] == "yes"


async def test_get_pairing_qr_returns_qr_ready():
    client = FakeGreenClient(qr_response={"type": "qrCode", "message": "b64png"})
    prov = GreenProvisioner(client, FakeResolver(ProviderCredentials(b"api-tok")))
    result = await prov.get_pairing_qr(ConnectionRef("green", "123"))
    assert result.outcome is PairingOutcome.QR_READY
    assert result.qr == PairingQr(image_base64="b64png")


async def test_get_pairing_qr_already_authorized():
    client = FakeGreenClient(qr_response={"type": "alreadyLogged"})
    prov = GreenProvisioner(client, FakeResolver(ProviderCredentials(b"api-tok")))
    result = await prov.get_pairing_qr(ConnectionRef("green", "123"))
    assert result.outcome is PairingOutcome.ALREADY_AUTHORIZED
    assert result.qr is None


async def test_get_pairing_qr_passkey_required():
    client = FakeGreenClient(
        qr_response={"type": "passkeyRequired", "message": "use passkey"}
    )
    prov = GreenProvisioner(client, FakeResolver(ProviderCredentials(b"api-tok")))
    result = await prov.get_pairing_qr(ConnectionRef("green", "123"))
    assert result.outcome is PairingOutcome.PASSKEY_REQUIRED
    assert result.message == "use passkey"


async def test_get_pairing_qr_timeout():
    client = FakeGreenClient(qr_response={"type": "timeout", "message": "timed out"})
    prov = GreenProvisioner(client, FakeResolver(ProviderCredentials(b"api-tok")))
    result = await prov.get_pairing_qr(ConnectionRef("green", "123"))
    assert result.outcome is PairingOutcome.TIMEOUT
    assert result.message == "timed out"


async def test_unpair_calls_logout():
    client = FakeGreenClient()
    prov = GreenProvisioner(client, FakeResolver(ProviderCredentials(b"api-tok")))
    await prov.unpair(ConnectionRef("green", "123"))
    assert client.calls[0][0] == "logout"


async def test_delete_connection_calls_delete_instance():
    client = FakeGreenClient()
    prov = GreenProvisioner(client, FakeResolver(ProviderCredentials(b"api-tok")))
    await prov.delete_connection(ConnectionRef("green", "123"))
    assert client.calls[0][0] == "delete_instance"


async def test_missing_credentials_raises_permanent_error():
    client = FakeGreenClient()
    prov = GreenProvisioner(client, FakeResolver(None))
    with pytest.raises(PermanentError):
        await prov.get_status(ConnectionRef("green", "123"))


def test_qr_result_helper_maps_each_type():
    assert (
        _qr_result({"type": "qrCode", "message": "x"}).outcome
        is PairingOutcome.QR_READY
    )
    assert (
        _qr_result({"type": "alreadyLogged"}).outcome
        is PairingOutcome.ALREADY_AUTHORIZED
    )
    assert (
        _qr_result({"type": "passkeyRequired", "message": "m"}).outcome
        is PairingOutcome.PASSKEY_REQUIRED
    )
    assert (
        _qr_result({"type": "timeout", "message": "m"}).outcome
        is PairingOutcome.TIMEOUT
    )
    assert (
        _qr_result({"type": "error", "message": "m"}).outcome is PairingOutcome.TIMEOUT
    )


def test_fake_resolver_satisfies_credential_resolver_protocol():
    assert isinstance(FakeResolver(None), CredentialResolver)
