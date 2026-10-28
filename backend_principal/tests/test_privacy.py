"""Autorización de datos (Ley 1581): se pide antes de atender; sin "Acepto" no
pasa nada por la IA; "No acepto" borra lo guardado; la política es pública."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.ai.ai_restaurant.user.tools.order_tools import confirm_order
from app.firestore_db.firebase_schemas import Conversations, Customers, Messages, Orders, WebhookEvents
from app.services import privacy, webhook_entry
from app.services.messaging import inbound_pipeline
from app.services.privacy_page import POLICY_VERSION, render_policy
from tests.conftest import DummyHandlers, seed_business, seed_conversation, seed_customer
from tests.test_flush_conversation import _incoming
from tests.test_order_state import _ready_order


def _new_customer(fake_store):
    business = seed_business(fake_store, phone="6041234567")
    customer = seed_customer(fake_store, business, data_consent_version=None)
    conversation = seed_conversation(fake_store, business, customer.customer_id, bot_activo=True)
    return business, customer, conversation


async def _ingest(business, customer, conversation, message, role="cliente"):
    with patch.object(inbound_pipeline, "get_vertical_handlers", return_value=DummyHandlers(role)):
        return await inbound_pipeline._process_customer_message(
            {"business": business, "customer": customer, "conversation": conversation, "message": message}
        )


def _click(fake_store, business, customer, conversation, choice):
    message = _incoming(fake_store, business, customer, conversation, choice, 30)
    message.type = "interactive"
    return message


async def test_first_message_asks_and_never_reaches_the_ai(fake_store):
    business, customer, conversation = _new_customer(fake_store)
    first = _incoming(fake_store, business, customer, conversation, "hola, ¿qué tienen hoy?", 0)
    second = _incoming(fake_store, business, customer, conversation, "¿hay alguien?", 5)

    with patch.object(privacy, "ask", new=AsyncMock(return_value=True)) as ask:
        assert await _ingest(business, customer, conversation, first) is None
        assert await _ingest(business, customer, conversation, second) is None

    ask.assert_awaited_once()  # no se repite en cada mensaje
    assert customer.data_consent_asked_at is not None
    # Quedan pendientes: si acepta, el bot los contesta.
    assert first.status != "completed" and second.status != "completed"


async def test_accepting_answers_what_they_had_written(fake_store):
    business, customer, conversation = _new_customer(fake_store)
    _incoming(fake_store, business, customer, conversation, "hola, ¿qué tienen hoy?", 0)
    click = _click(fake_store, business, customer, conversation, privacy.ACCEPT)

    pending = await _ingest(business, customer, conversation, click)

    assert pending is not None  # va al flush con el mensaje original y la nota
    assert click.content == privacy.ACCEPTED_NOTE
    saved = fake_store.stores[Customers][customer.customer_id]
    assert saved.data_consent_version == POLICY_VERSION and saved.data_consent_at is not None


async def test_declining_deletes_what_was_stored(fake_store):
    business, customer, conversation = _new_customer(fake_store)
    _incoming(fake_store, business, customer, conversation, "hola", 0)
    event = WebhookEvents()
    event.business_id = business.bussiness_id
    event.received_at = datetime.now(timezone.utc)
    event.raw_payload = {"entry": [{"changes": [{"value": {"contacts": [{"wa_id": customer.phone_number}]}}]}]}
    fake_store.seed(event)
    old = WebhookEvents()
    old.business_id = business.bussiness_id
    old.received_at = datetime.now(timezone.utc) - timedelta(days=5)
    old.raw_payload = {"wa_id": "otro"}
    fake_store.seed(old)
    click = _click(fake_store, business, customer, conversation, privacy.DECLINE)

    with patch("app.services.privacy.WhatsAppClient") as client_cls:
        client_cls.for_business.return_value.send_message = AsyncMock(return_value=True)
        assert await _ingest(business, customer, conversation, click) is None

    assert not fake_store.stores[Messages]
    assert not fake_store.stores[Conversations]
    assert customer.customer_id not in fake_store.stores[Customers]
    assert list(fake_store.stores[WebhookEvents].values()) == [old]
    farewell = client_cls.for_business.return_value.send_message.await_args.args
    assert "6041234567" in farewell[1] and "borramos" in farewell[1]


async def test_declining_keeps_the_customer_if_they_have_orders(fake_store):
    business, customer, conversation = _new_customer(fake_store)
    order = Orders()
    order.business_id = business.bussiness_id
    order.customer_id = customer.customer_id
    fake_store.seed(order)
    click = _click(fake_store, business, customer, conversation, privacy.DECLINE)

    with patch("app.services.privacy.WhatsAppClient") as client_cls:
        client_cls.for_business.return_value.send_message = AsyncMock(return_value=True)
        await _ingest(business, customer, conversation, click)

    assert customer.customer_id in fake_store.stores[Customers]
    assert fake_store.stores[Orders]
    assert not fake_store.stores[Messages]


async def test_customers_who_accepted_and_the_team_are_not_asked(fake_store):
    business = seed_business(fake_store)
    accepted = seed_customer(fake_store, business)  # el seed ya trae la versión vigente
    admin = seed_customer(fake_store, business, phone="573009998877", data_consent_version=None)
    with patch.object(privacy, "ask", new=AsyncMock()) as ask:
        for customer, role in ((accepted, "cliente"), (admin, "admin")):
            conversation = seed_conversation(fake_store, business, customer.customer_id, bot_activo=True)
            message = _incoming(fake_store, business, customer, conversation, "hola", 0)
            result = await _ingest(business, customer, conversation, message, role)
            if role == "cliente":
                assert result is not None  # sigue al flush normal
    ask.assert_not_awaited()  # al admin nunca se le piden los botones


async def test_confirm_order_still_refuses_without_consent(fake_store):
    ctx = await _ready_order(fake_store)
    fake_store.stores[Customers][ctx.state["customer_id"]].data_consent_version = None

    with patch.object(privacy, "ask", new=AsyncMock(return_value=True)):
        result = await confirm_order(ctx)

    assert result["ok"] is False and result["falta"] == ["autorizacion_datos"]
    assert not fake_store.stores[Orders]


def test_policy_shows_the_business_and_escapes_its_data():
    page = render_policy(
        {"name": "Edy", "legal_name": "Edy <b>SAS</b>", "phone": "3001234567"},
        {"restaurant_name": "Deliperritos"},
    )
    assert "Deliperritos" in page and "Edy &lt;b&gt;SAS&lt;/b&gt;" in page
    assert "3001234567" in page and "3B Technology" in page and POLICY_VERSION in page
    assert "datos sensibles" in page and "menores de edad" in page and "acceso no autorizado" in page
    assert "antes de atenderte" in page


def test_policy_page_rejects_odd_ids_without_touching_firestore():
    client = TestClient(webhook_entry.app)
    with patch("app.services.privacy_page._load") as load:
        assert client.get("/privacidad/..%2Fx").status_code == 404
        load.assert_not_called()
        load.return_value = (None, None)
        assert client.get("/privacidad/NoExiste123").status_code == 404
