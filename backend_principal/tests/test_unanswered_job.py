"""Detector de mensajes sin respuesta: dispara la alerta solo cuando un
negocio activo y abierto deja a un cliente esperando."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import admin_api_settings
from app.firestore_db.firebase_schemas import Configurations, Messages
from app.services import worker_entry
from app.services.messaging.unanswered_job import check_unanswered
from tests.conftest import seed_business, seed_configuration, seed_conversation, seed_customer

NOW = datetime.now(timezone.utc)


def _waiting(fake_store, minutes_ago=5, *, business_fields=None, **conversation_fields):
    business = seed_business(fake_store, **(business_fields or {}))
    customer = seed_customer(fake_store, business)
    written = NOW - timedelta(minutes=minutes_ago)
    conversation = seed_conversation(
        fake_store, business, customer.customer_id, **{"bot_activo": True, "ultimo_mensaje_cliente_at": written, **conversation_fields}
    )
    message = Messages()
    message.business_id = business.bussiness_id
    message.conversation_id = conversation.conversation_id
    message.content = "hola?"
    message.type = "text"
    message.created_at = written
    fake_store.seed(message)
    return business, conversation


def test_a_customer_waiting_too_long_raises_an_error(fake_store, caplog):
    business, conversation = _waiting(fake_store)

    result = check_unanswered(NOW)

    assert result["unanswered"] == [
        {"business_id": business.bussiness_id, "conversation_id": conversation.conversation_id, "messages": 1}
    ]
    record = next(r for r in caplog.records if getattr(r, "fields", {}).get("ops_event") == "unanswered_messages")
    assert record.levelname == "ERROR"


def test_no_alert_for_recent_answered_paused_inactive_or_closed(fake_store):
    _waiting(fake_store, minutes_ago=1)  # todavía en su turno normal
    _waiting(fake_store, ultimo_mensaje_respondido_at=NOW)  # ya contestado
    _waiting(fake_store, bot_activo=False)  # lo atiende un humano
    _waiting(fake_store, business_fields={"status": "inactive"})  # kill switch
    closed, _ = _waiting(fake_store)
    seed_configuration(fake_store, closed, opening_hours={"exceptions": [{"date": "2000-01-01", "closed": True}]})
    _waiting(fake_store, minutes_ago=30)  # fuera de la ventana: el claim ya no lo contesta

    assert check_unanswered(NOW)["unanswered"] == []


def test_the_job_endpoint_requires_the_api_key(fake_store):
    client = TestClient(worker_entry.app)
    url = "/api/v1/admin/jobs/check-unanswered"

    assert client.post(url, headers={"X-API-Key": "mala"}).status_code == 401
    assert client.post(url, headers={"X-API-Key": admin_api_settings.API_KEY}).json() == {"checked": 0, "unanswered": []}
