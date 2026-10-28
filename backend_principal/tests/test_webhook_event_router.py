from unittest.mock import patch

import pytest

from app.firestore_db.firebase_schemas import Conversations, Messages
from app.schemas.enum_schemas import MessageDirection, MessageOrigin, WebhookEventKind
from app.services.firestore_services.webhook_event_router import (
    WhatsAppWebhookEventRouter,
)
from tests.conftest import (
    customer_message_payload,
    echo_payload,
    seed_business,
    status_update_payload,
)


def test_customer_message_creates_business_customer_conversation_message(fake_store):
    seed_business(fake_store)
    payload = customer_message_payload(text="Hola")

    result = WhatsAppWebhookEventRouter().route(payload)

    assert result["event_kind"] == WebhookEventKind.MESSAGE.value
    assert not result.get("duplicate")
    assert result["message"].content == "Hola"
    assert result["conversation"].bot_activo is True
    assert result["conversation"].ultimo_mensaje_cliente_at is not None


def test_duplicate_customer_message_is_ignored(fake_store):
    seed_business(fake_store)
    payload = customer_message_payload(wamid="wamid.DUP1")

    first = WhatsAppWebhookEventRouter().route(payload)
    assert not first.get("duplicate")

    second = WhatsAppWebhookEventRouter().route(payload)
    assert second.get("duplicate") is True

    # Solo se creó UN mensaje, no dos.
    all_messages = list(fake_store.stores[Messages].values())
    assert len(all_messages) == 1


def test_status_update_marks_message_status_and_dedupes(fake_store):
    seed_business(fake_store)
    inbound = WhatsAppWebhookEventRouter().route(customer_message_payload(wamid="wamid.M1"))
    message = inbound["message"]
    message.whatsapp_message_id = "wamid.OUTBOUND1"
    message.status = "received"
    message.update()

    result = WhatsAppWebhookEventRouter().route(
        status_update_payload(wamid="wamid.OUTBOUND1", status="delivered")
    )

    assert result["event_kind"] == WebhookEventKind.STATUS.value
    updated = fake_store.stores[Messages][message.message_id]
    assert updated.status == "delivered"

    # Reenvío del MISMO status (Meta reintenta) no debe romper nada.
    repeat = WhatsAppWebhookEventRouter().route(
        status_update_payload(wamid="wamid.OUTBOUND1", status="delivered")
    )
    assert repeat["statuses"][0]["duplicate"] is True


def test_echo_pauses_bot_and_does_not_touch_client_window(fake_store):
    business = seed_business(fake_store)
    # El cliente ya escribió antes, así que ya existe conversación/customer.
    WhatsAppWebhookEventRouter().route(customer_message_payload(wamid="wamid.PREV"))

    result = WhatsAppWebhookEventRouter().route(echo_payload(text="Ya te atiendo"))

    assert result["event_kind"] == WebhookEventKind.ECHO.value
    conversation = result["conversation"]
    assert conversation.bot_activo is False
    assert conversation.motivo_handoff == "respuesta_manual"

    message = result["message"]
    assert message.direction == MessageDirection.SALIENTE.value
    assert message.origin == MessageOrigin.HUMANO.value


def test_echo_on_nonexistent_conversation_creates_customer_and_conversation(fake_store):
    seed_business(fake_store)
    # A diferencia del test anterior, el cliente NUNCA había escrito.
    result = WhatsAppWebhookEventRouter().route(
        echo_payload(to_phone="573005550000", text="Hola, ¿en qué te ayudo?")
    )

    assert result["event_kind"] == WebhookEventKind.ECHO.value
    assert result["customer"].phone_number == "573005550000"
    assert result["conversation"].bot_activo is False
    # Un echo no abre/extiende la ventana de 24h del cliente.
    assert result["conversation"].ultimo_mensaje_cliente_at is None


def test_echo_missing_business_returns_error_not_exception(fake_store):
    result = WhatsAppWebhookEventRouter().route(
        echo_payload(from_phone="00000000000")
    )
    assert result["event_kind"] == WebhookEventKind.ECHO.value
    assert "error" in result


def test_an_ingestion_that_failed_halfway_is_reprocessed_without_duplicating(fake_store):
    """Regresión: el WebhookEvent se registra ANTES de guardar el mensaje.
    Si Firestore fallaba en el medio, el reintento lo descartaba como
    duplicado y el mensaje se perdía para siempre."""
    seed_business(fake_store)
    payload = customer_message_payload(wamid="wamid.HALF", text="Hola")
    router = WhatsAppWebhookEventRouter()

    with patch.object(Conversations, "update", side_effect=RuntimeError("firestore caído")):
        with pytest.raises(RuntimeError):
            router.route(payload)

    retry = router.route(payload)

    assert not retry.get("duplicate") and retry["message"].content == "Hola"
    assert len(fake_store.stores[Messages]) == 1  # el reintento reutiliza el ya guardado
    assert router.route(payload).get("duplicate") is True  # ya completo: ahora sí es duplicado
