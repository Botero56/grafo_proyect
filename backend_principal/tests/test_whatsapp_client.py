"""Lo que se protege es cómo se interpreta la respuesta de la Graph API: el
wamid que devuelve es lo que se guarda en Messages.whatsapp_message_id y
con lo que después se cruzan los status (delivered/read)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import httpx

from app.services.whatsapp_client import whatsapp_client as whatsapp_client_module
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient


def _response(status_code: int, body: dict | None = None):
    response = MagicMock(status_code=status_code, text="error")
    response.json.return_value = body or {}
    return response


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    monkeypatch.setattr(whatsapp_client_module, "_RETRY_DELAYS", (0, 0))


@pytest.fixture
def client():
    return WhatsAppClient(token="t", phone_number_id="123")


async def test_text_is_posted_to_the_messages_endpoint(client):
    client.post = AsyncMock(return_value=_response(200, {"messages": [{"id": "wamid.X"}]}))

    assert await client.send_message("573001", "hola") is True

    client.post.assert_awaited_once_with(
        "123/messages",
        json={"messaging_product": "whatsapp", "to": "573001", "type": "text", "text": {"body": "hola"}},
    )


async def test_send_text_returns_the_wamid(client):
    client.post = AsyncMock(return_value=_response(200, {"messages": [{"id": "wamid.X"}]}))

    assert await client.send_text_and_get_id("573001", "hola") == "wamid.X"


async def test_send_text_returns_none_on_error_or_empty_response(client):
    client.post = AsyncMock(return_value=_response(400))
    assert await client.send_text_and_get_id("573001", "hola") is None

    client.post = AsyncMock(return_value=_response(200, {"messages": []}))
    assert await client.send_text_and_get_id("573001", "hola") is None


async def test_interactive_buttons_return_the_wamid(client):
    client.post = AsyncMock(return_value=_response(200, {"messages": [{"id": "wamid.BTN"}]}))

    wamid = await client.send_interactive_buttons("573001", "¿Confirmas?", [{"id": "ok", "title": "Sí"}])

    assert wamid == "wamid.BTN"
    sent = client.post.await_args.kwargs["json"]["interactive"]["action"]["buttons"]
    assert sent == [{"type": "reply", "reply": {"id": "ok", "title": "Sí"}}]


async def test_interactive_buttons_return_none_on_error(client):
    client.post = AsyncMock(return_value=_response(500))

    assert await client.send_interactive_buttons("573001", "x", []) is None


async def test_a_transient_meta_error_is_retried(client):
    client.post = AsyncMock(
        side_effect=[_response(503), _response(429), _response(200, {"messages": [{"id": "wamid.R"}]})]
    )

    assert await client.send_text_and_get_id("573001", "hola") == "wamid.R"
    assert client.post.await_count == 3


async def test_a_rejected_send_is_not_retried_and_logs_metas_code(client, caplog):
    """Regresión: el log solo traía el cuerpo crudo, sin código HTTP ni a
    qué número; el token vencido (190) no se distinguía de nada."""
    client.post = AsyncMock(
        return_value=_response(401, {"error": {"code": 190, "message": "Session has expired"}})
    )

    assert await client.send_text_and_get_id("573001234567", "hola") is None

    assert client.post.await_count == 1
    assert "http=401 code=190" in caplog.text and "***4567" in caplog.text
    assert "573001234567" not in caplog.text  # el teléfono completo no va a los logs


async def test_a_read_timeout_is_not_retried_because_the_message_may_have_left(client):
    client.post = AsyncMock(side_effect=httpx.ReadTimeout("lento"))

    assert await client.send_text_and_get_id("573001", "hola") is None
    assert client.post.await_count == 1


async def test_a_connection_error_is_retried_and_then_gives_up(client):
    client.post = AsyncMock(side_effect=httpx.ConnectError("sin red"))

    assert await client.send_text_and_get_id("573001", "hola") is None
    assert client.post.await_count == 3


async def test_a_username_customer_is_sent_by_bsuid_in_recipient(client):
    # Prueba real 2026-10-19: sin `from`, Kapso rechazaba con
    # "recipient phone number or BSUID missing" y el cliente no recibía nada.
    client.post = AsyncMock(return_value=_response(200, {"messages": [{"id": "wamid.U"}]}))

    assert await client.send_message("CO.1780159176423859", "hola") is True

    sent = client.post.await_args.kwargs["json"]
    assert sent["recipient"] == "CO.1780159176423859"
    assert "to" not in sent
