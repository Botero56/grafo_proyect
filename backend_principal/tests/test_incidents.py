"""Incidencias de entrega: el domiciliario reporta, se le escribe al
cliente, el temporizador reintenta y escala al admin."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.ai.ai_restaurant.courier import courier_tools
from app.ai.ai_restaurant.user.tools import customer_tools
from app.ai.core.agent_runner import AgentReply
from app.firestore_db.firebase_schemas import Incidents, Messages, Orders, OrderStatusHistory
from app.schemas.enum_schemas import IncidentStatus, OrderStatus
from app.services import worker_entry
from app.services.conversation.handoff_service import local_phone
from app.services.delivery import couriers, incidents, tracking
from app.services.delivery.couriers import CourierMessageHandler
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.firestore_webhook_service import find_active_conversation
from app.services.messaging import inbound_pipeline
from tests.conftest import (
    DummyHandlers,
    FakeToolContext,
    ingest_and_reply,
    seed_business,
    seed_configuration,
    seed_conversation,
    seed_customer,
)

CARLOS = "573001110000"
CLIENT = "573005550000"


@pytest.fixture
def setup(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, couriers=[{"phone": CARLOS, "name": "Carlos"}], manager_phone="573009999999")
    courier = seed_customer(fake_store, business, phone=CARLOS, name="Carlos")
    customer = seed_customer(fake_store, business, phone=CLIENT, name="Ana")
    seed_conversation(fake_store, business, courier.customer_id)
    seed_conversation(fake_store, business, customer.customer_id)
    return business, courier, customer


def _order(business, customer, status=OrderStatus.EN_COCINA.value) -> Orders:
    now = datetime.now(timezone.utc)
    order = FireStoreCRUDService().create(
        Orders, business_id=business.bussiness_id, customer_id=customer.customer_id, status=status,
        order_total=20000, payment_method="efectivo", created_at=now, confirmed_at=now, delivery_address="Calle 1",
    )
    tracking.assign_daily_code(order, now)
    return order


@contextmanager
def _io(within_24h=True):
    """WhatsApp, la cola, la nota en la sesión del cliente y el aviso al admin."""
    with patch.object(incidents, "OutboundMessageService") as outbound, patch.object(incidents, "WhatsAppClient"), patch.object(
        incidents.cloud_tasks_client, "enqueue_incident_timeout"
    ) as timer, patch.object(incidents, "wrote_within_24h", return_value=within_24h), patch.object(
        incidents.shared_agent_runner(), "append_human_message", new=AsyncMock()
    ) as note, patch.object(incidents.HandoffService, "notify_admins", new=AsyncMock(return_value=True)) as admin:
        sent = MagicMock()
        sent.whatsapp_message_id = "wamid.x"
        outbound.return_value.send_and_persist = AsyncMock(return_value=sent)
        yield {"send": outbound.return_value.send_and_persist, "timer": timer, "note": note, "admin": admin}


def _sent_to(io, phone) -> list[str]:
    return [c.kwargs["text"] for c in io["send"].await_args_list if c.kwargs["to_phone"] == phone]


def _events(fake_store, order) -> list[str]:
    return [e.event_type for e in fake_store.stores[OrderStatusHistory].values() if e.order_id == order.order_id]


async def _report(business, courier, code=1):
    return await incidents.open_incident(business, {"phone": CARLOS, "name": "Carlos"}, courier.customer_id, code, "no me abren")


async def test_courier_report_contacts_the_customer_and_starts_the_timer(fake_store, setup):
    business, courier, customer = setup
    order = _order(business, customer)
    with _io() as io:
        result = await _report(business, courier)

    assert result["ok"] and "le escribí al cliente del pedido #1" in result["texto"] and "4 min" in result["texto"]
    [incident] = fake_store.stores[Incidents].values()
    assert (incident.status, incident.attempts, incident.customer_phone) == (IncidentStatus.ESPERANDO_CLIENTE.value, 1, CLIENT)
    assert "pedido #1 está en tu dirección" in _sent_to(io, CLIENT)[0]
    io["note"].assert_awaited_once()  # la IA del cliente sabe de qué le hablan
    io["timer"].assert_called_once_with(incident.incident_id, 1, 240)
    assert Orders.collection.get(order.order_id).active_incident_id == incident.incident_id
    assert _events(fake_store, order) == ["incidencia_abierta"]


async def test_no_answer_retries_once_then_escalates_to_the_admin(fake_store, setup):
    business, courier, customer = setup
    order = _order(business, customer)
    with _io() as io:
        await _report(business, courier)
        incident = next(iter(fake_store.stores[Incidents].values()))
        assert await incidents.on_timeout(incident.incident_id, 1) == "reintento"
        assert await incidents.on_timeout(incident.incident_id, 1) == "sin_cambios"  # el mismo temporizador repetido
        assert await incidents.on_timeout(incident.incident_id, 2) == "escalada"
        assert await incidents.on_timeout(incident.incident_id, 2) == "sin_cambios"

    assert incident.status == IncidentStatus.ESCALADA.value
    assert "¿Sigues ahí?" in _sent_to(io, CLIENT)[1]
    admin_text = io["admin"].await_args.args[1]
    assert "pedido #1" in admin_text and "El cliente no responde" in admin_text and local_phone(CLIENT) in admin_text
    to_courier = "".join(_sent_to(io, CARLOS))
    assert "Ya avisamos al administrador" in to_courier
    assert local_phone(CLIENT) not in to_courier and CLIENT not in to_courier  # nunca al domiciliario
    assert _events(fake_store, order) == ["incidencia_abierta", "incidencia_reintento", "incidencia_escalada"]


async def test_a_resolved_incident_is_never_escalated_by_a_late_timer(fake_store, setup):
    business, courier, customer = setup
    _order(business, customer)
    with _io() as io:
        await _report(business, courier)
        incident = next(iter(fake_store.stores[Incidents].values()))
        incident.status = IncidentStatus.RESUELTA.value
        assert await incidents.on_timeout(incident.incident_id, 1) == "sin_cambios"
    io["admin"].assert_not_awaited()


async def test_a_second_report_of_the_same_order_reuses_the_incident(fake_store, setup):
    business, courier, customer = setup
    _order(business, customer)
    with _io() as io:
        await _report(business, courier)
        again = await _report(business, courier)

    assert again["ok"] and "Ya le escribí" in again["texto"]
    assert len(fake_store.stores[Incidents]) == 1
    assert len(_sent_to(io, CLIENT)) == 1 and io["timer"].call_count == 1


@pytest.mark.parametrize(
    "case, reason",
    [("missing", "no_existe"), ("delivered", "ya_entregada"), ("own", "es_tu_pedido")],
)
async def test_invalid_orders_are_rejected(fake_store, setup, case, reason):
    business, courier, customer = setup
    if case == "delivered":
        _order(business, customer, status=OrderStatus.ENTREGADO_CONFIRMADO.value)
    elif case == "own":
        _order(business, courier)
    with _io() as io:
        assert await _report(business, courier) == {"ok": False, "reason": reason}
    assert fake_store.stores[Incidents] == {}
    io["send"].assert_not_awaited()


async def test_customer_outside_the_24h_window_escalates_right_away(fake_store, setup):
    business, courier, customer = setup
    _order(business, customer)
    with _io(within_24h=False) as io:
        result = await _report(business, courier)

    assert "No le puedo escribir" in result["texto"]
    assert next(iter(fake_store.stores[Incidents].values())).status == IncidentStatus.ESCALADA.value
    assert _sent_to(io, CLIENT) == [] and "más de 24 h" in io["admin"].await_args.args[1]
    io["timer"].assert_not_called()


async def test_two_invalid_codes_in_the_agent_escalate_to_the_admin(fake_store, setup):
    business, courier, _ = setup
    ctx = FakeToolContext(business.bussiness_id, courier.customer_id)
    with _io() as io:
        first = await courier_tools.reportar_incidencia(ctx, 7, "no me abren")
        second = await courier_tools.reportar_incidencia(ctx, 8, "no me abren")

    assert first["reason"] == "no_existe" and "revise el número" in first["indicacion"]
    assert second == {"ok": False, "reason": "escalado_al_admin"}
    assert "no dio un número de comanda válido" in io["admin"].await_args.args[1]


async def test_clear_incident_message_is_opened_without_the_agent(fake_store, setup):
    business, courier, customer = setup
    _order(business, customer)
    message = Messages()
    message.type, message.content, message.conversation_id = "text", "Tengo un problema con el pedido 1, no me abren", "conv1"
    with _io(), patch.object(couriers, "OutboundMessageService") as reply:
        reply.return_value.send_and_persist = AsyncMock()
        assert await CourierMessageHandler(AsyncMock()).maybe_handle(business, courier, message)
    assert "le escribí al cliente del pedido #1" in reply.return_value.send_and_persist.await_args.kwargs["text"]
    assert len(fake_store.stores[Incidents]) == 1


def test_a_courier_cannot_settle_his_own_order(fake_store, setup):
    business, courier, customer = setup
    _order(business, courier)
    _order(business, customer)
    result = tracking.settle(business.bussiness_id, CARLOS, [1, 2], "liquidacion")
    assert (result["own"], result["ok"]) == ([1], [2])


def test_the_timer_task_calls_on_timeout():
    with patch.object(worker_entry.incidents, "on_timeout", new=AsyncMock(return_value="reintento")) as on_timeout:
        response = TestClient(worker_entry.app).post("/tasks/incident-timeout", json={"incident_id": "i1", "attempt": 1})
    assert response.status_code == 200
    on_timeout.assert_awaited_once_with("i1", 1)


# --- Respuesta del cliente (F3) ---------------------------------------------------


async def _open(fake_store, business, courier, customer, io) -> Incidents:
    _order(business, customer)
    await _report(business, courier)
    return next(iter(fake_store.stores[Incidents].values()))


async def test_customer_answer_reaches_the_courier_and_resolves_the_incident(fake_store, setup):
    business, courier, customer = setup
    with _io() as io:
        incident = await _open(fake_store, business, courier, customer, io)
        result = await incidents.resolve_from_customer(business, incident, "otra_direccion", "torre 3 apto 402, llámenme al 300 123 4567")
        late_timer = await incidents.on_timeout(incident.incident_id, 1)

    assert result["resultado"] == "instruccion_enviada"
    [instruction] = _sent_to(io, CARLOS)
    assert "Pedido #1" in instruction and "torre 3 apto 402" in instruction
    assert "300 123 4567" not in instruction and "[número]" in instruction  # ningún teléfono al domiciliario
    assert Incidents.collection.get(incident.incident_id).status == IncidentStatus.RESUELTA.value
    assert late_timer == "sin_cambios"
    io["admin"].assert_not_awaited()


@pytest.mark.parametrize("action", ["cancelar", "cambio_pago", "otro_telefono"])
async def test_cancel_payment_or_another_phone_go_to_the_admin(fake_store, setup, action):
    business, courier, customer = setup
    with _io() as io:
        incident = await _open(fake_store, business, courier, customer, io)
        result = await incidents.resolve_from_customer(business, incident, action, "3001234567")

    assert result["resultado"] == "escalado"
    assert Incidents.collection.get(incident.incident_id).status == IncidentStatus.ESCALADA.value
    assert "3001234567" in io["admin"].await_args.args[1]  # al admin sí
    assert "3001234567" not in "".join(_sent_to(io, CARLOS))


async def test_an_answer_after_escalation_still_reaches_the_courier_and_the_admin(fake_store, setup):
    business, courier, customer = setup
    with _io() as io:
        incident = await _open(fake_store, business, courier, customer, io)
        await incidents.on_timeout(incident.incident_id, 1)
        await incidents.on_timeout(incident.incident_id, 2)  # escalada
        result = await incidents.resolve_from_customer(business, incident, "bajando")

    assert result["resultado"] == "instruccion_enviada"
    assert "ya sale a recibirlo" in _sent_to(io, CARLOS)[-1]
    assert "respondió" in io["admin"].await_args.args[1]
    assert Incidents.collection.get(incident.incident_id).status == IncidentStatus.RESUELTA.value


async def test_customer_tool_without_an_active_incident(fake_store, setup):
    business, _, customer = setup
    ctx = FakeToolContext(business.bussiness_id, customer.customer_id)
    assert await customer_tools.responder_incidencia("bajando", ctx) == {"ok": False, "reason": "sin_incidencia"}


async def test_the_customer_turn_carries_the_incident_note(fake_store, setup):
    business, courier, customer = setup
    with _io() as io:
        await _open(fake_store, business, courier, customer, io)
    conversation = find_active_conversation(business.bussiness_id, customer.customer_id)
    message = FireStoreCRUDService().create(
        Messages, business_id=business.bussiness_id, customer_id=customer.customer_id,
        conversation_id=conversation.conversation_id, content="ya bajo", type="text", status="received",
        created_at=datetime.now(timezone.utc) - timedelta(seconds=30),
    )
    context = {"business": business, "customer": customer, "conversation": conversation, "message": message, "message_delay_seconds": None}
    with patch.object(inbound_pipeline, "get_vertical_handlers", return_value=DummyHandlers("cliente", customer.customer_id)), patch.object(
        inbound_pipeline._agent_runner, "get_reply", new=AsyncMock(return_value=AgentReply(text="Listo"))
    ) as reply, patch.object(inbound_pipeline.menu_image, "send_menu_image", new=AsyncMock(return_value=False)), patch.object(
        inbound_pipeline, "OutboundMessageService"
    ) as outbound:
        outbound.return_value.send_and_persist = AsyncMock()
        await ingest_and_reply(context)

    text = reply.await_args.kwargs["message_text"]
    assert "novedad de entrega en su pedido #1" in text and text.endswith("ya bajo")


async def test_paused_customer_chat_escalates_the_incident(fake_store, setup):
    business, courier, customer = setup
    with _io() as io:
        incident = await _open(fake_store, business, courier, customer, io)
        conversation = find_active_conversation(business.bussiness_id, customer.customer_id)
        conversation.bot_activo, conversation.motivo_handoff = False, "respuesta_manual"
        conversation.ultimo_mensaje_humano_at = datetime.now(timezone.utc)
        message = FireStoreCRUDService().create(
            Messages, business_id=business.bussiness_id, customer_id=customer.customer_id,
            conversation_id=conversation.conversation_id, content="ya bajo", type="text", status="received",
            created_at=datetime.now(timezone.utc),
        )
        context = {"business": business, "customer": customer, "conversation": conversation, "message": message, "message_delay_seconds": None}
        with patch.object(inbound_pipeline._handoff_service, "notify_human", new=AsyncMock(return_value=True)):
            await inbound_pipeline._process_customer_message(context)

    assert Incidents.collection.get(incident.incident_id).status == IncidentStatus.ESCALADA.value
    assert "chat con el equipo" in io["admin"].await_args.args[1]


# --- La liquidación resuelve la novedad ----------------------------------------------


@pytest.mark.parametrize("escalated", [False, True])
async def test_settling_the_order_resolves_its_incident(fake_store, setup, escalated):
    """Sin que el admin la cierre: "entregué el 1" o "liquidar 1 2 3" al final del día."""
    business, courier, customer = setup
    with _io() as io:
        incident = await _open(fake_store, business, courier, customer, io)
        if escalated:
            await incidents.on_timeout(incident.incident_id, 1)
            await incidents.on_timeout(incident.incident_id, 2)
        assert tracking.settle(business.bussiness_id, CARLOS, [1], "liquidacion")["ok"] == [1]
        late_timer = await incidents.on_timeout(incident.incident_id, 2 if escalated else 1)

    stored = Incidents.collection.get(incident.incident_id)
    assert stored.status == IncidentStatus.RESUELTA.value and stored.resolved_at is not None
    assert late_timer == "sin_cambios"
    assert _events(fake_store, Orders.collection.get(incident.order_id))[-1] == "incidencia_resuelta"
    # Resuelta: la IA del cliente ya no la ve como novedad activa.
    assert incidents.find_active_for_customer(business.bussiness_id, customer.customer_id) is None


async def test_bajando_does_not_repeat_the_customer_words(fake_store, setup):
    business, courier, customer = setup
    with _io() as io:
        incident = await _open(fake_store, business, courier, customer, io)
        await incidents.resolve_from_customer(business, incident, "bajando", "Ya baj9")
    assert _sent_to(io, CARLOS)[-1] == "📦 Pedido #1: el cliente ya sale a recibirlo."
