"""Webhooks de coexistencia: history, smb_app_state_sync y el ORDEN de los
echoes frente a los mensajes del cliente.

Cloud Tasks no garantiza orden, así que la pausa del bot se decide con los
timestamps de WhatsApp (cuándo se escribió), nunca con cuándo se procesó.
"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.config import whatsapp_settings
from app.firestore_db.firebase_schemas import Conversations, Customers, Messages
from app.schemas.enum_schemas import MessageOrigin, MessageStatus
from app.services import webhook_entry
from app.services.conversation.handoff_service import HandoffService
from app.services.firestore_services.webhook_event_router import WhatsAppWebhookEventRouter
from app.services.messaging.webhook_payload import content_event_id, split_events, split_history
from tests.conftest import (
    BUSINESS_PHONE,
    CUSTOMER_PHONE,
    PHONE_NUMBER_ID,
    WABA_ID,
    DummyHandlers,
    customer_message_payload,
    echo_payload,
    seed_business,
    seed_configuration,
    seed_conversation,
    seed_customer,
)

from app.services.messaging import inbound_pipeline

OTHER_CUSTOMER = "573005556677"


def _envelope(field: str, value: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": WABA_ID, "changes": [{"field": field, "value": value}]}],
    }


def _metadata() -> dict:
    return {"display_phone_number": BUSINESS_PHONE, "phone_number_id": PHONE_NUMBER_ID}


def _history_payload(threads=None, progress=55, phase=0) -> dict:
    if threads is None:
        threads = [
            {
                "id": CUSTOMER_PHONE,
                "messages": [
                    {
                        "from": CUSTOMER_PHONE,
                        "id": "wamid.H1",
                        "timestamp": "1739230900",
                        "type": "text",
                        "text": {"body": "¿Tienen almuerzo?"},
                        "history_context": {"status": "READ"},
                    },
                    {
                        "from": BUSINESS_PHONE,
                        "id": "wamid.H2",
                        "timestamp": "1739230955",
                        "type": "text",
                        "text": {"body": "Sí, bandeja paisa"},
                        "history_context": {"status": "READ"},
                    },
                ],
            },
            {
                "id": OTHER_CUSTOMER,
                "messages": [
                    {
                        "from": OTHER_CUSTOMER,
                        "id": "wamid.H3",
                        "timestamp": "1739230999",
                        "type": "text",
                        "text": {"body": "Gracias"},
                    }
                ],
            },
        ]
    return _envelope(
        "history",
        {
            "messaging_product": "whatsapp",
            "metadata": _metadata(),
            "history": [{"metadata": {"phase": phase, "chunk_order": 1, "progress": progress}, "threads": threads}],
        },
    )


def _signed_headers(raw: str) -> dict:
    digest = hmac.new(whatsapp_settings.APP_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


# ----------------------------------------------------------------------
# Webhook (ingress): cola aparte e idempotencia
# ----------------------------------------------------------------------


def test_history_is_split_per_customer_and_goes_to_the_history_queue():
    body = _history_payload()
    raw = json.dumps(body)
    client = TestClient(webhook_entry.app)

    with patch.object(webhook_entry.cloud_tasks_client, "enqueue_sync_event") as enqueue, patch.object(
        webhook_entry.cloud_tasks_client, "enqueue_event"
    ) as enqueue_message:
        response = client.post("/webhook", content=raw, headers=_signed_headers(raw))

    assert response.status_code == 200
    enqueue_message.assert_not_called()
    assert enqueue.call_count == 2
    thread_ids = [
        c.args[0]["entry"][0]["changes"][0]["value"]["history"][0]["threads"][0]["id"] for c in enqueue.call_args_list
    ]
    assert thread_ids == [CUSTOMER_PHONE, OTHER_CUSTOMER]


def test_a_redelivered_history_gets_the_same_task_names():
    first = [content_event_id(p) for p in split_history(_history_payload())]
    again = [content_event_id(p) for p in split_history(_history_payload())]
    assert first == again
    assert len(set(first)) == 2


def test_contacts_mixed_with_messages_in_one_delivery_are_not_lost():
    message = customer_message_payload()
    contacts = _envelope("smb_app_state_sync", {"metadata": _metadata(), "state_sync": []})
    message["entry"][0]["changes"].append(contacts["entry"][0]["changes"][0])

    fields = [p["entry"][0]["changes"][0]["field"] for p in split_events(message)]

    assert fields == ["messages", "smb_app_state_sync"]


def test_if_the_history_queue_fails_meta_is_asked_to_retry():
    raw = json.dumps(_history_payload())
    client = TestClient(webhook_entry.app)
    with patch.object(webhook_entry.cloud_tasks_client, "enqueue_sync_event", side_effect=RuntimeError("caído")):
        response = client.post("/webhook", content=raw, headers=_signed_headers(raw))
    assert response.status_code == 502


# ----------------------------------------------------------------------
# Worker: history
# ----------------------------------------------------------------------


def test_history_is_stored_as_past_messages_that_nobody_must_answer(fake_store):
    seed_business(fake_store)

    for piece in split_history(_history_payload()):
        result = WhatsAppWebhookEventRouter().route(piece)
        assert result["status"] == "history_imported"

    messages = {m.whatsapp_message_id: m for m in fake_store.stores[Messages].values()}
    assert set(messages) == {"wamid.H1", "wamid.H2", "wamid.H3"}
    assert all(m.origin == MessageOrigin.HISTORIAL.value for m in messages.values())
    assert all(m.status == MessageStatus.COMPLETED.value for m in messages.values())
    assert messages["wamid.H1"].direction == "entrante"
    assert messages["wamid.H2"].direction == "saliente"
    assert messages["wamid.H1"].created_at == datetime.fromtimestamp(1739230900, tz=timezone.utc)

    # Nada del historial cuenta como pendiente de respuesta.
    for conversation in fake_store.stores[Conversations].values():
        assert inbound_pipeline._claim_pending_messages(conversation) == []
        assert conversation.bot_activo is True


def test_a_repeated_history_task_does_not_duplicate_messages(fake_store):
    seed_business(fake_store)
    piece = split_history(_history_payload())[0]

    WhatsAppWebhookEventRouter().route(piece)
    second = WhatsAppWebhookEventRouter().route(piece)

    assert second.get("duplicate") is True
    assert len(fake_store.stores[Messages]) == 2


def test_declined_history_does_not_fail(fake_store):
    seed_business(fake_store)
    payload = _envelope(
        "history",
        {"metadata": _metadata(), "history": [{"errors": [{"code": 2593109, "title": "History sync is turned off"}]}]},
    )

    result = WhatsAppWebhookEventRouter().route(payload)

    assert not result.get("error")


# ----------------------------------------------------------------------
# Worker: smb_app_state_sync
# ----------------------------------------------------------------------


def test_contacts_create_customers_with_their_names(fake_store):
    business = seed_business(fake_store)
    seed_customer(fake_store, business, phone=CUSTOMER_PHONE, name="Cliente Nuevo")
    payload = _envelope(
        "smb_app_state_sync",
        {
            "metadata": _metadata(),
            "state_sync": [
                {"type": "contact", "contact": {"full_name": "Ana Gómez", "phone_number": "+" + CUSTOMER_PHONE}, "action": "add"},
                {"type": "contact", "contact": {"full_name": "Luis", "phone_number": OTHER_CUSTOMER}, "action": "add"},
                {"type": "contact", "contact": {"full_name": "Borrado", "phone_number": "573000000001"}, "action": "remove"},
            ],
        },
    )

    result = WhatsAppWebhookEventRouter().route(payload)

    assert result["added"] == 2
    names = {c.phone_number: c.name for c in fake_store.stores[Customers].values()}
    assert names == {CUSTOMER_PHONE: "Ana Gómez", OTHER_CUSTOMER: "Luis"}
    # Contactos no abren conversaciones.
    assert fake_store.stores[Conversations] == {}


# ----------------------------------------------------------------------
# Orden: echoes vs. mensajes del cliente
# ----------------------------------------------------------------------


def _echo_at(when: datetime, wamid="wamid.E1") -> dict:
    payload = echo_payload(wamid=wamid)
    payload["entry"][0]["changes"][0]["value"]["message_echoes"][0]["timestamp"] = str(int(when.timestamp()))
    return payload


def test_the_pause_is_dated_by_the_echo_not_by_processing_time(fake_store):
    seed_business(fake_store)
    written = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=3)

    result = WhatsAppWebhookEventRouter().route(_echo_at(written))

    conversation = result["conversation"]
    assert conversation.bot_activo is False
    assert conversation.pausado_desde == written
    assert conversation.ultimo_mensaje_humano_at == written


def test_a_late_echo_older_than_the_reactivation_does_not_pause_again(fake_store):
    business = seed_business(fake_store)
    customer = seed_customer(fake_store, business)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    seed_conversation(fake_store, business, customer.customer_id, bot_activo=True, bot_reactivado_at=now)

    result = WhatsAppWebhookEventRouter().route(_echo_at(now - timedelta(minutes=2)))

    assert result["conversation"].bot_activo is True


def test_an_older_echo_does_not_move_back_the_last_human_message(fake_store):
    business = seed_business(fake_store)
    customer = seed_customer(fake_store, business)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    seed_conversation(fake_store, business, customer.customer_id, bot_activo=False, ultimo_mensaje_humano_at=now)

    result = WhatsAppWebhookEventRouter().route(_echo_at(now - timedelta(minutes=5)))

    assert result["conversation"].ultimo_mensaje_humano_at == now


def test_a_customer_message_written_before_the_echo_never_reactivates_the_bot(fake_store):
    conversation = Conversations()
    human_at = datetime.now(timezone.utc)
    conversation.bot_activo = False
    conversation.ultimo_mensaje_humano_at = human_at

    # El cliente escribió 10 min ANTES de que la encargada respondiera,
    # aunque su mensaje se procese después (llegó tarde a la cola).
    assert HandoffService().should_auto_reactivate(conversation, human_at - timedelta(minutes=10)) is False
    # Y uno escrito 1 min DESPUÉS sí reactiva (pasaron más de 30 s).
    assert HandoffService().should_auto_reactivate(conversation, human_at + timedelta(minutes=1)) is True


async def test_a_flush_waiting_in_the_queue_does_not_answer_if_the_owner_replied(fake_store):
    business = seed_business(fake_store)
    customer = seed_customer(fake_store, business)
    conversation = seed_conversation(
        fake_store, business, customer.customer_id, bot_activo=False, motivo_handoff="respuesta_manual"
    )
    message = Messages()
    message.business_id = business.bussiness_id
    message.customer_id = customer.customer_id
    message.conversation_id = conversation.conversation_id
    message.content = "hola"
    message.type = "text"
    message.created_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    fake_store.seed(message)

    with patch.object(inbound_pipeline, "get_vertical_handlers", return_value=DummyHandlers("cliente")), patch.object(
        inbound_pipeline, "_generate_and_send_reply", AsyncMock()
    ) as reply:
        answered = await inbound_pipeline.flush_conversation(
            business_id=business.bussiness_id,
            customer_id=customer.customer_id,
            conversation_id=conversation.conversation_id,
        )

    assert answered is False
    reply.assert_not_called()
    # Reclamados: cuando el bot se reactive no se le pasan a la IA.
    assert inbound_pipeline._claim_pending_messages(conversation) == []


def test_a_history_task_that_failed_halfway_is_resumed_by_the_retry(fake_store):
    seed_business(fake_store)
    piece = split_history(_history_payload())[0]
    router = WhatsAppWebhookEventRouter()

    with patch.object(router, "_save_history_message", side_effect=RuntimeError("Firestore caído")):
        try:
            router.route(piece)
        except RuntimeError:
            pass

    result = WhatsAppWebhookEventRouter().route(piece)

    assert result.get("duplicate") is not True
    assert result["saved"] == 2


def test_an_echo_to_an_admin_chat_never_pauses_it(fake_store):
    """Prueba real 2026-10-19: desde el número del negocio se le mandó el
    link del catálogo al admin, su chat quedó en pausa y la foto de menú
    que mandó después se perdió."""
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, admin_phone_numbers=[CUSTOMER_PHONE])

    result = WhatsAppWebhookEventRouter().route(echo_payload())

    assert result["conversation"].bot_activo is not False
