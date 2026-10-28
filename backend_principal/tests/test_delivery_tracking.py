"""Domicilios: consecutivo del día, parser y liquidación de domiciliarios,
señales del cliente, presunto entregado y cierre diario."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.ai_restaurant import get_agent
from app.ai.ai_restaurant.courier import courier_tools
from app.ai.ai_restaurant.courier.agent import courier_agent
from app.ai.ai_restaurant.role_resolver import resolve_user_role
from app.ai.core.agent_runner import FALLBACK_REPLY, AgentReply
from app.firestore_db.firebase_schemas import Messages, Orders, OrderStatusHistory
from app.schemas.enum_schemas import OrderStatus
from app.services.delivery import couriers, tracking
from app.services.delivery.couriers import CourierMessageHandler, parse_courier_command
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.messaging import inbound_pipeline
from tests.conftest import seed_business, seed_configuration, seed_conversation, seed_customer

# 12:00 en Bogotá.
NOW = datetime(2026, 10, 16, 17, 0, tzinfo=timezone.utc)
CARLOS, PEDRO = "573001110000", "573002220000"
COURIERS = [{"phone": CARLOS, "name": "Carlos"}, {"phone": PEDRO, "name": "Pedro"}]
LIQ = "liquidacion"


@pytest.fixture
def biz(fake_store):
    business = seed_business(fake_store)
    configuration = seed_configuration(fake_store, business, couriers=COURIERS, tiempo_entrega_estimado_min=40, hora_cierre_domicilios="21:00")
    return business, configuration


def _order(business, method="efectivo", total=20000, now=NOW, customer_id="c1", **extra) -> Orders:
    order = FireStoreCRUDService().create(
        Orders, business_id=business.bussiness_id, customer_id=customer_id, status=OrderStatus.EN_COCINA.value,
        order_total=total, payment_method=method, created_at=now, confirmed_at=now, delivery_address="Calle 1", **extra,
    )
    tracking.assign_daily_code(order, now)
    return order


def _events(fake_store, order) -> list:
    return [e for e in fake_store.stores[OrderStatusHistory].values() if e.order_id == order.order_id]


# --- Parser -----------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("liquidar 12 15 18 22 31", ([12, 15, 18, 22, 31], None, LIQ)),
        ("Liquidar 12, 15, 18 base 50000", ([12, 15, 18], 50000, LIQ)),
        ("LIQUIDAR #12,#15 base $50.000", ([12, 15], 50000, LIQ)),
        ("liquidación los pedidos 3 y 4", ([3, 4], None, LIQ)),
        ("Entregué 18", ([18], None, "confirmacion_tiempo_real")),
        ("entregue 18 19 18", ([18, 19], None, "confirmacion_tiempo_real")),
        # Mensajes reales de la prueba del 2026-10-16.
        ("Entregado el pedido 1", ([1], None, "confirmacion_tiempo_real")),
        ("Entregué el pedido 1 con base de 75 mil", ([1], 75000, "confirmacion_tiempo_real")),
        ("liquidar 3 4 con base de $75.000.", ([3, 4], 75000, LIQ)),
        ("entregué 5 base 50k", ([5], 50000, "confirmacion_tiempo_real")),
        # Saludo o muletilla antes del verbo (prueba real 2026-10-17: "Ya entregué el 7" iba al agente).
        ("Ya entregué el 7", ([7], None, "confirmacion_tiempo_real")),
        ("Hola, liquidar 3 4", ([3, 4], None, LIQ)),
        ("Listo jefe, entregué el pedido 2!", ([2], None, "confirmacion_tiempo_real")),
    ],
)
def test_parser_understands_tolerant_commands(text, expected):
    command = parse_courier_command(text)
    assert (command["codes"], command["base"], command["source"]) == expected


@pytest.mark.parametrize("text", ["hola", "liquidar", "entregué el de la calle 45", "liquidar 12 mañana", "quiero liquidar 12", "liquidar 1000", "De base tengo $75,000."])
def test_parser_leaves_anything_unclear_to_the_agent(text):
    assert parse_courier_command(text) is None


# --- Consecutivo --------------------------------------------------------------

def test_daily_code_is_consecutive_per_business_and_restarts_each_day(fake_store, biz):
    business, _ = biz
    other = seed_business(fake_store)
    assert [_order(business).daily_code for _ in range(3)] == [1, 2, 3]
    assert _order(other).daily_code == 1
    assert _order(business, now=NOW + timedelta(days=1)).daily_code == 1


def test_daily_code_retry_keeps_the_same_code(fake_store, biz):
    order = _order(biz[0])
    assert tracking.assign_daily_code(order, NOW) == order.daily_code == 1
    assert _order(biz[0]).daily_code == 2


def test_concurrent_daily_codes_never_repeat(fake_store, biz):
    business, _ = biz
    orders = [
        FireStoreCRUDService().create(Orders, business_id=business.bussiness_id, status="en_cocina", created_at=NOW)
        for _ in range(20)
    ]
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda o: tracking.assign_daily_code(o, NOW), orders))
    assert sorted(codes) == list(range(1, 21))


# --- Liquidación ----------------------------------------------------------------

def test_settle_attributes_valid_codes_and_reports_unknown(fake_store, biz):
    business, _ = biz
    a, b = _order(business), _order(business, method="transferencia", total=30000)
    result = tracking.settle(business.bussiness_id, CARLOS, [1, 2, 99], LIQ, NOW)
    assert result == {"ok": [1, 2], "mine": [], "unknown": [99], "own": [], "conflicts": []}
    for order in (a, b):
        stored = Orders.collection.get(order.order_id)
        assert (stored.status, stored.courier_phone, stored.delivered_at) == (OrderStatus.ENTREGADO_CONFIRMADO.value, CARLOS, NOW)
        [event] = _events(fake_store, order)
        assert (event.event_type, event.source, event.actor, event.previous_status) == ("liquidado", LIQ, CARLOS, "en_cocina")
    assert tracking.courier_summary(business.bussiness_id, CARLOS, NOW) == {"count": 2, "codes": [1, 2], "cash": 20000, "transfer": 30000}


def test_settle_is_idempotent_for_the_same_courier(fake_store, biz):
    business, _ = biz
    order = _order(business)
    tracking.settle(business.bussiness_id, CARLOS, [1], LIQ, NOW)
    assert tracking.settle(business.bussiness_id, CARLOS, [1], LIQ, NOW) == {"ok": [], "mine": [1], "unknown": [], "own": [], "conflicts": []}
    assert len(_events(fake_store, order)) == 1


def test_settle_never_overwrites_another_courier_and_records_the_conflict_once(fake_store, biz):
    business, _ = biz
    order = _order(business)
    tracking.settle(business.bussiness_id, CARLOS, [1], LIQ, NOW)
    first = tracking.settle(business.bussiness_id, PEDRO, [1], LIQ, NOW)
    again = tracking.settle(business.bussiness_id, PEDRO, [1], LIQ, NOW)
    assert first["conflicts"] == [{"code": 1, "other": CARLOS, "new": True}]
    assert again["conflicts"] == [{"code": 1, "other": CARLOS, "new": False}]
    stored = Orders.collection.get(order.order_id)
    assert (stored.courier_phone, stored.conflict_courier_phones) == (CARLOS, [PEDRO])


def test_settlement_accumulates_across_messages_and_other_days_are_unknown(fake_store, biz):
    business, _ = biz
    _order(business, now=NOW - timedelta(days=1))  # #1 de ayer
    _order(business), _order(business, method="transferencia", total=15000, cash_due=5000)
    tracking.settle(business.bussiness_id, CARLOS, [1], LIQ, NOW)
    tracking.settle(business.bussiness_id, CARLOS, [2], LIQ, NOW)
    assert tracking.courier_summary(business.bussiness_id, CARLOS, NOW) == {"count": 2, "codes": [1, 2], "cash": 25000, "transfer": 10000}
    assert tracking.settle(business.bussiness_id, CARLOS, [3], LIQ, NOW)["unknown"] == [3]


def test_late_settlement_after_closing_is_allowed(fake_store, biz):
    business, _ = biz
    order = _order(business)
    order.status = OrderStatus.NO_LIQUIDADO.value
    assert tracking.settle(business.bussiness_id, CARLOS, [1], LIQ, NOW)["ok"] == [1]


# --- Manejador de domiciliarios -----------------------------------------------------

def _message(text, type_="text"):
    message = Messages()
    message.type, message.content, message.conversation_id = type_, text, "conv1"
    return message


async def test_courier_command_is_answered_without_the_agent(fake_store, biz):
    business, _ = biz
    now = datetime.now(timezone.utc)  # el manejador usa la hora real
    _order(business, now=now), _order(business, now=now)
    tracking.settle(business.bussiness_id, PEDRO, [2], LIQ, now)
    courier = seed_customer(fake_store, business, phone=CARLOS)
    client = AsyncMock()
    with patch.object(couriers, "OutboundMessageService") as outbound:
        outbound.return_value.send_and_persist = AsyncMock()
        handled = await CourierMessageHandler(client).maybe_handle(business, courier, _message("liquidar 1, 2 base 50000"))
    assert handled
    reply = outbound.return_value.send_and_persist.await_args.kwargs["text"]
    assert "✅ Registradas: #1" in reply and "#2 ya la liquidó Pedro" in reply
    assert "Base $50.000 + efectivo = $70.000" in reply
    assert client.send_message.await_args.args[0] == PEDRO


def test_role_resolver_tells_admin_courier_and_courier_as_customer_apart(fake_store, biz):
    business, configuration = biz
    configuration.admin_phone_numbers = [PEDRO]  # admin gana sobre domiciliario
    courier = seed_customer(fake_store, business, phone=CARLOS)
    customer = seed_customer(fake_store, business)
    resolve = lambda c: resolve_user_role(business.bussiness_id, c.phone_number if c else PEDRO, c.customer_id if c else None)
    assert resolve(None)["role"] == "admin"
    assert resolve(customer) == {"role": "cliente", "customer_id": customer.customer_id}
    assert resolve(courier)["role"] == "domiciliario"
    courier.courier_as_customer_date = tracking.service_date(business.bussiness_id)
    assert resolve(courier) == {"role": "cliente", "customer_id": courier.customer_id, "courier": True}
    courier.courier_as_customer_date = "2020-01-01"  # el modo cliente vence al cambiar de día
    assert resolve(courier)["role"] == "domiciliario"


async def test_unclear_courier_messages_and_non_couriers_go_to_an_agent(fake_store, biz):
    business, _ = biz
    customer = seed_customer(fake_store, business)
    courier = seed_customer(fake_store, business, phone=CARLOS)
    with patch.object(couriers, "OutboundMessageService") as outbound:
        outbound.return_value.send_and_persist = AsyncMock()
        handler = CourierMessageHandler(AsyncMock())
        assert not await handler.maybe_handle(business, customer, _message("liquidar 1"))
        assert not await handler.maybe_handle(business, courier, _message("De base tengo $75,000."))
        assert not await handler.maybe_handle(business, courier, _message("", type_="image"))
    outbound.return_value.send_and_persist.assert_not_awaited()


async def test_courier_voice_note_is_transcribed_and_settled(fake_store, biz):
    business, _ = biz
    _order(business, now=datetime.now(timezone.utc))
    courier = seed_customer(fake_store, business, phone=CARLOS)
    message = _message("[audio]", type_="audio")
    with patch.object(couriers, "OutboundMessageService") as outbound, patch.object(
        couriers.AudioProcessor, "process", new=AsyncMock()
    ), patch.object(couriers, "transcribe", new=AsyncMock(return_value="Entregué el pedido 1 con base de 75 mil")):
        outbound.return_value.send_and_persist = AsyncMock()
        assert await CourierMessageHandler(AsyncMock()).maybe_handle(business, courier, message)
    reply = outbound.return_value.send_and_persist.await_args.kwargs["text"]
    assert "✅ Registradas: #1" in reply and "Base $75.000" in reply
    assert message.content == "Entregué el pedido 1 con base de 75 mil"


async def test_unheard_courier_voice_note_goes_to_the_agent_untouched(fake_store, biz):
    business, _ = biz
    courier = seed_customer(fake_store, business, phone=CARLOS)
    message = _message("[audio]", type_="audio")
    with patch.object(couriers.AudioProcessor, "process", new=AsyncMock(side_effect=couriers.AudioProcessingError("muy largo"))):
        assert not await CourierMessageHandler(AsyncMock()).maybe_handle(business, courier, message)
    assert message.content == "[audio]"  # el flush la reintenta y el agente pide repetirla


# --- Agente de domiciliarios -------------------------------------------------------------

def _tool_context(business, customer):
    context = MagicMock()
    context.state = {"business_id": business.bussiness_id, "customer_id": customer.customer_id}
    return context


async def test_courier_tools_settle_summarize_and_switch_to_customer_mode(fake_store, biz):
    business, _ = biz
    now = datetime.now(timezone.utc)
    _order(business, now=now), _order(business, method="transferencia", total=30000, now=now)
    courier = seed_customer(fake_store, business, phone=CARLOS)
    context = _tool_context(business, courier)
    with patch.object(courier_tools, "WhatsAppClient"):
        settled = await courier_tools.liquidar_comandas(context, [1, 2, 7], base=50000)
    assert settled["ok"] and "✅ Registradas: #1, #2" in settled["texto"] and "No existen hoy: #7" in settled["texto"]
    assert "Base $50.000 + efectivo = $70.000" in settled["texto"]
    summary = courier_tools.mi_resumen_de_hoy(context)["texto"]
    assert "2 comanda(s) (#1, #2)" in summary and "Transferencia: $30.000" in summary

    assert courier_tools.usar_modo_cliente(context) == {"ok": True}
    assert resolve_user_role(business.bussiness_id, CARLOS, courier.customer_id)["role"] == "cliente"


async def test_courier_tools_refuse_someone_who_is_not_a_courier(fake_store, biz):
    business, _ = biz
    context = _tool_context(business, seed_customer(fake_store, business))
    assert (await courier_tools.liquidar_comandas(context, [1]))["reason"] == "no_es_domiciliario"
    assert courier_tools.usar_modo_cliente(context)["reason"] == "no_es_domiciliario"


async def test_courier_agent_gets_its_own_session_and_help_text_when_it_fails(fake_store, biz):
    business, _ = biz
    assert get_agent("domiciliario") is courier_agent
    courier = seed_customer(fake_store, business, phone=CARLOS)
    conversation = seed_conversation(fake_store, business, courier.customer_id)
    with patch.object(inbound_pipeline._agent_runner, "get_reply", AsyncMock(return_value=AgentReply(text=FALLBACK_REPLY))) as reply, patch.object(
        inbound_pipeline, "OutboundMessageService"
    ) as outbound:
        outbound.return_value.send_and_persist = AsyncMock()
        await inbound_pipeline._generate_and_send_reply(
            whatsapp_client=AsyncMock(), business=business, customer=courier, conversation=conversation,
            agent=courier_agent, message_text="tengo un problema", role="domiciliario",
        )
    assert reply.await_args.kwargs["session_scope"] == "domiciliario"
    assert outbound.return_value.send_and_persist.await_args.kwargs["text"] == couriers.HELP_TEXT


# --- Señales del cliente y presunto entregado -----------------------------------------------

async def test_inquiry_marks_complaint_and_alerts_the_admin(fake_store, biz):
    business, _ = biz
    customer = seed_customer(fake_store, business)
    order = _order(business, customer_id=customer.customer_id)
    with patch.object(tracking.HandoffService, "notify_admins", AsyncMock(return_value=True)) as notify:
        result = await tracking.report_customer_update(business, customer, "pregunta_por_pedido", "¿ya viene?", NOW + timedelta(minutes=50))
    assert result == {"ok": True, "admin_notified": True}
    assert Orders.collection.get(order.order_id).status == OrderStatus.CON_RECLAMO.value
    text = notify.await_args.args[1]
    assert "#1" in text and "Calle 1" in text and "50 min" in text and "¿ya viene?" in text


async def test_passive_signal_is_recorded_without_attribution(fake_store, biz):
    business, _ = biz
    customer = seed_customer(fake_store, business)
    order = _order(business, customer_id=customer.customer_id)
    await tracking.report_customer_update(business, customer, "ya_llego", "ya llegó, gracias", NOW)
    stored = Orders.collection.get(order.order_id)
    assert (stored.status, stored.courier_phone) == (OrderStatus.EN_COCINA.value, None)
    [event] = _events(fake_store, order)
    assert (event.event_type, event.source) == ("ya_llego", "senal_cliente")


async def test_sweep_presumes_delivery_only_after_the_estimate_and_never_for_held_payments(fake_store, biz):
    business, _ = biz
    ready = _order(business)
    held = _order(business, method="transferencia", payment_status="pendiente_comprobante")
    late = _order(business, now=NOW + timedelta(minutes=30))
    await tracking.sweep(NOW + timedelta(minutes=41))
    statuses = [Orders.collection.get(o.order_id).status for o in (ready, held, late)]
    assert statuses == ["presunto_entregado", "en_cocina", "en_cocina"]


# --- Cierre diario ------------------------------------------------------------------------

async def test_daily_close_sends_one_message_and_marks_unsettled(fake_store, biz):
    business, _ = biz
    _order(business), _order(business, method="transferencia", total=30000), _order(business, total=12000)
    tracking.settle(business.bussiness_id, CARLOS, [1], LIQ, NOW)
    tracking.settle(business.bussiness_id, PEDRO, [1], LIQ, NOW)
    after_cut = NOW.replace(hour=2) + timedelta(days=1)  # 21:00 Bogotá
    with patch.object(tracking.HandoffService, "notify_admins", AsyncMock(return_value=True)) as notify:
        assert (await tracking.sweep(NOW))["closed"] == []  # antes del corte
        assert len((await tracking.sweep(after_cut))["closed"]) == 1
        assert (await tracking.sweep(after_cut + timedelta(minutes=10)))["closed"] == []
    [(_, text, kind)] = [c.args for c in notify.await_args_list]
    assert kind == "cierre_diario"
    assert "Comandas del día: 3" in text and "Liquidadas: 1" in text
    assert "• Carlos: 1 comanda(s) · Efectivo $20.000 · Transferencia " in text
    assert "#2, #3 (efectivo)" in text and "Efectivo sin liquidar: $12.000" in text
    assert "#1 liquidado por varios: Carlos, Pedro" in text
    states = sorted(o.status for o in fake_store.stores[Orders].values())
    assert states == ["entregado_confirmado", "no_liquidado", "no_liquidado"]


async def test_yesterday_is_closed_even_if_the_job_missed_the_cut(fake_store, biz):
    business, _ = biz
    _order(business)
    with patch.object(tracking.HandoffService, "notify_admins", AsyncMock(return_value=True)):
        result = await tracking.sweep(NOW + timedelta(days=1))
    assert result["closed"] == [{"business_id": business.bussiness_id, "date": "2026-10-16"}]


def test_admin_report_shows_what_each_courier_delivered(fake_store, biz):
    from app.ai.ai_restaurant.admin.admin_tools import liquidacion_domiciliarios
    from tests.conftest import FakeToolContext

    business, _ = biz
    now = datetime.now(timezone.utc)
    _order(business, now=now), _order(business, method="transferencia", total=30000, now=now), _order(business, now=now)
    tracking.settle(business.bussiness_id, CARLOS, [1, 2], LIQ, now)

    result = liquidacion_domiciliarios(FakeToolContext(business.bussiness_id))

    assert result["ok"]
    assert "• Carlos: 2 comanda(s) (#1, #2) · Efectivo $20.000 · Transferencia $30.000" in result["texto"]
    assert "Sin liquidar: #3" in result["texto"] and "Pedro" not in result["texto"]
    assert liquidacion_domiciliarios(FakeToolContext(business.bussiness_id), fecha="ayer") == {"ok": False, "reason": "fecha_invalida"}


# --- El admin le avisa al cliente cuánto falta ---------------------------------

async def test_admin_tells_the_asking_customer_how_long_it_takes(fake_store, biz):
    from app.ai.ai_restaurant.admin import admin_tools
    from app.services.delivery import incidents
    from tests.conftest import FakeToolContext

    business, _ = biz
    now = datetime.now(timezone.utc)
    ana = seed_customer(fake_store, business, name="Ana")
    seed_conversation(fake_store, business, ana.customer_id)
    order = _order(business, customer_id=ana.customer_id, now=now)
    other = seed_customer(fake_store, business, name="Luis", phone="573009990000")
    _order(business, customer_id=other.customer_id, now=now)  # no pregunta: no cuenta
    ctx = FakeToolContext(business.bussiness_id)

    with patch.object(tracking.HandoffService, "notify_admins", AsyncMock(return_value=True)):
        await tracking.report_customer_update(business, ana, "pregunta_por_pedido", "¿cuánto demora?", now)
    with patch.object(incidents, "send_to_customer", AsyncMock(return_value=True)) as send, \
         patch.object(admin_tools.shared_agent_runner(), "append_human_message", AsyncMock()) as note:
        assert (await admin_tools.avisar_tiempo_entrega(ctx, 0))["reason"] == "minutos_invalidos"
        result = await admin_tools.avisar_tiempo_entrega(ctx, 20)  # sin nombre: la única que preguntó

    assert result == {"ok": True, "customer_name": "Ana", "pedido": order.daily_code, "customer_notified": True}
    assert send.await_args.args[1] == ana.customer_id
    assert f"#{order.daily_code}" in send.await_args.args[2] and "20 minutos" in send.await_args.args[2]
    assert note.await_args.kwargs["by_system"] is True


async def test_admin_eta_without_name_asks_which_customer_when_several_ask(fake_store, biz):
    from app.ai.ai_restaurant.admin import admin_tools
    from tests.conftest import FakeToolContext

    business, _ = biz
    now = datetime.now(timezone.utc)
    for name, phone in (("Ana", "573001234567"), ("Luis", "573009990000")):
        customer = seed_customer(fake_store, business, name=name, phone=phone)
        _order(business, customer_id=customer.customer_id, now=now)
        with patch.object(tracking.HandoffService, "notify_admins", AsyncMock(return_value=True)):
            await tracking.report_customer_update(business, customer, "pregunta_por_pedido", "¿ya viene?", now)

    result = await admin_tools.avisar_tiempo_entrega(FakeToolContext(business.bussiness_id), 15)

    assert result["reason"] == "cliente_no_identificado" and sorted(result["preguntando"]) == ["Ana", "Luis"]
