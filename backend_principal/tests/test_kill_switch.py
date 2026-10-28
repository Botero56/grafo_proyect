"""Kill switch por negocio (Businesses.status): apagado, los mensajes se
guardan sin pasar por la IA; no afecta a otros negocios y al reactivar solo
se contesta lo nuevo."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.config import admin_api_settings
from app.firestore_db.firebase_schemas import Businesses
from app.schemas.enum_schemas import MessageStatus, is_business_active
from app.services import worker_entry
from app.services.messaging import inbound_pipeline
from tests.conftest import DummyHandlers, seed_business, seed_conversation, seed_customer
from tests.test_flush_conversation import _incoming

client = TestClient(worker_entry.app)
HEADERS = {"X-API-Key": admin_api_settings.API_KEY}


def _context(fake_store, **business_fields):
    business = seed_business(fake_store, **business_fields)
    customer = seed_customer(fake_store, business)
    conversation = seed_conversation(fake_store, business, customer.customer_id, bot_activo=True)
    return business, customer, conversation


def _ingest(business, customer, conversation, message):
    return inbound_pipeline._process_customer_message(
        {"business": business, "customer": customer, "conversation": conversation, "message": message}
    )


def test_missing_status_counts_as_active():
    business = Businesses()
    assert is_business_active(business)
    business.status = "inactive"
    assert not is_business_active(business)


async def test_a_paused_business_stores_the_message_without_ai_and_others_keep_working(fake_store):
    paused = _context(fake_store, status="inactive")
    active = _context(fake_store, name="Otro")
    paused_message = _incoming(fake_store, *paused, "hola", 0)
    active_message = _incoming(fake_store, *active, "hola", 0)

    with patch.object(inbound_pipeline, "get_vertical_handlers", return_value=DummyHandlers("cliente")):
        assert await _ingest(*paused, paused_message) is None
        assert await _ingest(*active, active_message) is not None

    assert paused_message.status == MessageStatus.COMPLETED.value
    assert active_message.status != MessageStatus.COMPLETED.value


async def test_flush_of_a_paused_business_never_calls_the_agent(fake_store):
    business, customer, conversation = _context(fake_store)
    _incoming(fake_store, business, customer, conversation, "hola", -60)
    business.status = "inactive"  # se apagó con la tanda en cola

    with patch.object(inbound_pipeline._agent_runner, "get_reply", new=AsyncMock()) as get_reply:
        answered = await inbound_pipeline.flush_conversation(
            business.bussiness_id, customer.customer_id, conversation.conversation_id
        )

    assert answered is False
    get_reply.assert_not_awaited()


async def test_after_reactivating_only_new_messages_are_answered(fake_store):
    business, customer, conversation = _context(fake_store, status="inactive")
    old = _incoming(fake_store, business, customer, conversation, "mensaje de cuando estaba apagado", 0)
    with patch.object(inbound_pipeline, "get_vertical_handlers", return_value=DummyHandlers("cliente")):
        await _ingest(business, customer, conversation, old)

        response = client.put(
            f"/api/v1/admin/businesses/{business.bussiness_id}/bot-status",
            json={"status": "active", "actor": "juan@j2ys.co"},
            headers=HEADERS,
        )
        assert response.status_code == 200
        new = _incoming(fake_store, business, customer, conversation, "ya volvieron?", 5)
        assert await _ingest(business, customer, conversation, new) is not None

    assert [m.content for m in inbound_pipeline._claim_pending_messages(conversation)] == ["ya volvieron?"]


def test_bot_status_endpoint_records_who_and_when(fake_store):
    business = seed_business(fake_store)
    url = f"/api/v1/admin/businesses/{business.bussiness_id}/bot-status"

    response = client.put(url, json={"status": "inactive", "actor": "juan@j2ys.co", "reason": "no pagó"}, headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "inactive" and body["changed_by"] == "juan@j2ys.co" and body["reason"] == "no pagó"
    stored = fake_store.stores[Businesses][business.bussiness_id]
    assert stored.status == "inactive" and stored.bot_status_changed_at is not None
    assert client.get(url, headers=HEADERS).json()["changed_by"] == "juan@j2ys.co"


def test_bot_status_requires_actor_and_a_known_status(fake_store):
    business = seed_business(fake_store)
    url = f"/api/v1/admin/businesses/{business.bussiness_id}/bot-status"

    assert client.put(url, json={"status": "inactive"}, headers=HEADERS).status_code == 422
    assert client.put(url, json={"status": "borrado", "actor": "x"}, headers=HEADERS).status_code == 422
    assert client.put(url, json={"status": "inactive", "actor": "x"}, headers={"X-API-Key": "mala"}).status_code == 401
