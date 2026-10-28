"""Conexión por Kapso: firma de sus webhooks y envío por su proxy de Graph API.

Lo que se protege: (1) un webhook solo pasa si está firmado con el secreto
de SU vía (Meta o Kapso), (2) los negocios marcados "kapso" envían a
api.kapso.ai con X-API-Key y los demás siguen igual que antes, y (3) la
API key de Kapso nunca viaja a otro dominio."""

import hashlib
import hmac
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.datastructures import Headers

from app.config import whatsapp_settings
from app.services.whatsapp_client import whatsapp_client as whatsapp_client_module
from app.services.whatsapp_client.signature import (
    verify_kapso_signature,
    verify_meta_signature,
    verify_webhook_signature,
)
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient, provider_for

BODY = b'{"object":"whatsapp_business_account","entry":[]}'
KAPSO_SECRET = "kapso-secret"
META_SECRET = "meta-secret"


def _hex(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _verify(headers: dict, body: bytes = BODY, kapso_secret: str = KAPSO_SECRET) -> bool:
    return verify_webhook_signature(
        body, Headers(headers), app_secret=META_SECRET, kapso_secret=kapso_secret
    )


def _business(provider=None, phone_number_id="1344009488801289"):
    return SimpleNamespace(
        bussiness_id="biz-1", whatsapp_phone_number_id=phone_number_id, whatsapp_provider=provider
    )


def _response(status_code: int, body: dict | None = None):
    response = MagicMock(status_code=status_code, text="error")
    response.json.return_value = body or {}
    return response


@pytest.fixture
def kapso_settings(monkeypatch):
    monkeypatch.setattr(whatsapp_settings, "KAPSO_API_KEY", "kapso-key")
    monkeypatch.setattr(whatsapp_settings, "KAPSO_API_BASE_URL", "https://api.kapso.ai/meta/whatsapp")
    monkeypatch.setattr(whatsapp_settings, "KAPSO_API_VERSION", "v24.0")
    monkeypatch.setattr(whatsapp_settings, "WHATSAPP_PROVIDER", "meta")


# --- Firma -----------------------------------------------------------------


def test_kapso_signature_without_prefix_passes():
    assert _verify({"X-Webhook-Signature": _hex(BODY, KAPSO_SECRET)}) is True


def test_kapso_signature_with_prefix_is_tolerated():
    assert _verify({"x-webhook-signature": f"sha256={_hex(BODY, KAPSO_SECRET)}"}) is True


def test_kapso_signature_with_meta_secret_fails():
    assert _verify({"X-Webhook-Signature": _hex(BODY, META_SECRET)}) is False


def test_kapso_signature_tampered_body_fails():
    assert _verify({"X-Webhook-Signature": _hex(BODY, KAPSO_SECRET)}, body=BODY + b" ") is False


def test_kapso_header_without_configured_secret_fails():
    # Sin secreto, un HMAC con clave vacía lo podría calcular cualquiera.
    assert _verify({"X-Webhook-Signature": _hex(BODY, "")}, kapso_secret="") is False


def test_meta_signature_still_passes():
    assert _verify({"X-Hub-Signature-256": f"sha256={_hex(BODY, META_SECRET)}"}) is True


def test_meta_signature_with_kapso_secret_fails():
    assert _verify({"X-Hub-Signature-256": f"sha256={_hex(BODY, KAPSO_SECRET)}"}) is False


def test_kapso_header_takes_precedence_and_is_not_bypassed_by_a_valid_meta_header():
    headers = {
        "X-Webhook-Signature": "basura",
        "X-Hub-Signature-256": f"sha256={_hex(BODY, META_SECRET)}",
    }
    assert _verify(headers) is False


def test_no_signature_fails():
    assert _verify({}) is False


def test_empty_secrets_reject_even_matching_empty_key_hmacs():
    assert verify_meta_signature(BODY, f"sha256={_hex(BODY, '')}", "") is False
    assert verify_kapso_signature(BODY, _hex(BODY, ""), "") is False


# --- Elección de proveedor ---------------------------------------------------


def test_provider_defaults_to_global_setting(kapso_settings, monkeypatch):
    assert provider_for(_business()) == "meta"
    monkeypatch.setattr(whatsapp_settings, "WHATSAPP_PROVIDER", "kapso")
    assert provider_for(_business()) == "kapso"


def test_business_override_wins_over_global(kapso_settings, monkeypatch):
    assert provider_for(_business("kapso")) == "kapso"
    monkeypatch.setattr(whatsapp_settings, "WHATSAPP_PROVIDER", "kapso")
    assert provider_for(_business("meta")) == "meta"


def test_unknown_provider_falls_back_to_meta(kapso_settings):
    assert provider_for(_business("twilio")) == "meta"


def test_business_without_the_field_uses_global(kapso_settings):
    legacy = SimpleNamespace(bussiness_id="b", whatsapp_phone_number_id="1")
    assert provider_for(legacy) == "meta"


# --- Cliente Kapso -----------------------------------------------------------


def test_for_business_kapso_uses_kapso_base_and_api_key(kapso_settings):
    client = WhatsAppClient.for_business(_business("kapso"))

    assert client.provider == "kapso"
    assert client._resolve_url("1344009488801289/messages") == (
        "https://api.kapso.ai/meta/whatsapp/v24.0/1344009488801289/messages"
    )
    assert client._headers == {"X-API-Key": "kapso-key"}


def test_for_business_meta_is_unchanged(kapso_settings, monkeypatch):
    monkeypatch.setattr(whatsapp_settings, "TEMP_TOKEN_WPP", "meta-token")
    client = WhatsAppClient.for_business(_business(None, phone_number_id="999"))

    assert client.provider == "meta"
    assert client._resolve_url("999/messages").startswith("https://graph.facebook.com/")
    assert client._headers == {"Authorization": "Bearer meta-token"}


def _tenant(ref="projects/p/secrets/kapso-api-key--biz-1"):
    business = _business("kapso")
    business.kapso_api_key_secret = ref
    return business


def test_each_tenant_sends_with_its_own_key_from_secret_manager(kapso_settings):
    with patch.object(whatsapp_client_module.secrets, "get_secret_or_none", return_value="key-del-tenant") as read:
        client = WhatsAppClient.for_business(_tenant())

    read.assert_called_once_with("projects/p/secrets/kapso-api-key--biz-1")
    assert client._headers == {"X-API-Key": "key-del-tenant"}


def test_unreadable_tenant_secret_fails_closed_instead_of_using_the_global_key(kapso_settings):
    with patch.object(whatsapp_client_module.secrets, "get_secret_or_none", return_value=None):
        client = WhatsAppClient.for_business(_tenant())

    assert client._headers == {"X-API-Key": ""}


def test_tenant_without_secret_falls_back_to_the_env_key_with_one_warning(kapso_settings, caplog):
    whatsapp_client_module._warned_env_fallback.clear()

    first = WhatsAppClient.for_business(_business("kapso"))
    WhatsAppClient.for_business(_business("kapso"))

    assert first._headers == {"X-API-Key": "kapso-key"}
    assert caplog.text.count("Tenant sin secreto en Secret Manager") == 1
    assert "kapso-key" not in caplog.text


async def test_a_401_rereads_the_rotated_key_and_retries_once(kapso_settings):
    client = WhatsAppClient(token="vieja", phone_number_id="1", provider="kapso", secret_ref="ref")
    request = AsyncMock(side_effect=[_response(401), _response(200, {"messages": [{"id": "wamid.R"}]})])

    with patch("httpx.AsyncClient.request", new=request), patch.object(
        whatsapp_client_module.secrets, "get_secret_or_none", return_value="rotada"
    ):
        assert await client.send_text_and_get_id("573001", "hola") == "wamid.R"

    assert [c.kwargs["headers"] for c in request.await_args_list] == [{"X-API-Key": "vieja"}, {"X-API-Key": "rotada"}]


async def test_a_401_with_the_same_key_is_not_retried(kapso_settings):
    client = WhatsAppClient(token="misma", phone_number_id="1", provider="kapso", secret_ref="ref")
    request = AsyncMock(return_value=_response(401))

    with patch("httpx.AsyncClient.request", new=request), patch.object(
        whatsapp_client_module.secrets, "get_secret_or_none", return_value="misma"
    ):
        assert await client.send_text_and_get_id("573001", "hola") is None

    assert request.await_count == 1


def test_kapso_api_key_never_goes_to_another_host(kapso_settings):
    client = WhatsAppClient(token="kapso-key", phone_number_id="1", provider="kapso")
    assert client._headers_for("https://api.kapso.ai/media_download?token=x") == {"X-API-Key": "kapso-key"}
    assert client._headers_for("https://lookaside.fbsbx.com/whatsapp_business/x") == {}
    assert client._headers_for("https://storage.example.com/file") == {}


def test_meta_keeps_bearer_on_lookaside_download_url():
    client = WhatsAppClient(token="meta-token", phone_number_id="1")
    assert client._headers_for("https://lookaside.fbsbx.com/whatsapp_business/x") == {
        "Authorization": "Bearer meta-token"
    }


async def test_kapso_send_posts_cloud_api_payload_and_reads_wamid(kapso_settings):
    client = WhatsAppClient(token="kapso-key", phone_number_id="1344009488801289", provider="kapso")
    client.post = AsyncMock(return_value=_response(200, {"messages": [{"id": "wamid.K"}]}))

    assert await client.send_text_and_get_id("573001", "hola") == "wamid.K"
    client.post.assert_awaited_once_with(
        "1344009488801289/messages",
        json={"messaging_product": "whatsapp", "to": "573001", "type": "text", "text": {"body": "hola"}},
    )


async def test_wamid_is_read_from_a_data_envelope(kapso_settings):
    client = WhatsAppClient(token="kapso-key", phone_number_id="1", provider="kapso")
    client.post = AsyncMock(return_value=_response(200, {"data": {"messages": [{"id": "wamid.D"}]}}))

    assert await client.send_text_and_get_id("573001", "hola") == "wamid.D"


# Forma real de GET /{media_id} en Kapso (2026-10-12): trae la URL de Meta
# (inservible sin token de Meta) y la de Kapso.
_KAPSO_MEDIA = {
    "id": "m1",
    "messaging_product": "whatsapp",
    "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=m1",
    "mime_type": "image/jpeg",
    "file_size": 245924,
    "download_url": "https://api.kapso.ai/meta/whatsapp/media_download?token=t",
}


async def test_kapso_media_metadata_sends_phone_number_id_and_prefers_kapso_url(kapso_settings):
    client = WhatsAppClient(token="kapso-key", phone_number_id="1344009488801289", provider="kapso")
    client.get = AsyncMock(return_value=_response(200, _KAPSO_MEDIA))

    metadata = await client.get_media_metadata("m1")

    client.get.assert_awaited_once_with("m1", params={"phone_number_id": "1344009488801289"})
    # Consumidores como MenuImageDownloader leen metadata["url"].
    assert metadata["url"] == _KAPSO_MEDIA["download_url"]
    assert metadata["meta_url"] == _KAPSO_MEDIA["url"]
    assert metadata["file_size"] == 245924


async def test_meta_media_metadata_is_unchanged():
    client = WhatsAppClient(token="meta-token", phone_number_id="1")
    body = {"id": "m1", "url": "https://lookaside.fbsbx.com/x"}
    client.get = AsyncMock(return_value=_response(200, body))

    assert await client.get_media_metadata("m1") == body
    client.get.assert_awaited_once_with("m1", params=None)


async def test_media_download_follows_kapso_download_url(kapso_settings):
    client = WhatsAppClient(token="kapso-key", phone_number_id="1", provider="kapso")
    content = MagicMock(status_code=200, content=b"bytes")
    client.get = AsyncMock(side_effect=[_response(200, _KAPSO_MEDIA), content])

    assert await client.download_media("m1") == b"bytes"
    assert client.get.await_args_list[1].args[0] == _KAPSO_MEDIA["download_url"]


async def test_kapso_string_error_is_logged_without_crashing(kapso_settings, caplog):
    client = WhatsAppClient(token="kapso-key", phone_number_id="1", provider="kapso")
    client.get = AsyncMock(return_value=_response(404, {"error": "WhatsApp configuration not found"}))

    assert await client.get_media_metadata("m1") is None
    assert "WhatsApp configuration not found" in caplog.text
