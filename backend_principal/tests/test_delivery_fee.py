"""Dirección -> zona -> tarifa -> total. La tarifa la pone siempre el código
(catálogo de zonas o cotización de un humano), nunca la IA: sin tarifa no
hay total, y sin total no hay pedido confirmado ni pago."""

from unittest.mock import AsyncMock, patch

import pytest

from app.ai.ai_restaurant.admin.admin_tools import add_delivery_zone, set_delivery_quote
from app.ai.ai_restaurant.user.tools.customer_tools import (
    _delivery_fee,
    request_delivery_quote,
    resolve_delivery_fee,
    save_order,
)
from app.firestore_db.firebase_schemas import Configurations, Messages, Orders
from app.schemas.enum_schemas import DeliveryStatus
from app.services.delivery import quotes
from app.services.delivery.zones import find_zone, matching_zones
from tests.conftest import (
    FakeToolContext,
    seed_business,
    seed_active_menu,
    seed_configuration,
    seed_conversation,
    seed_customer,
)

ZONES = [
    {"nombre": "Cerro Claro", "tarifa": 3000, "alias": []},
    {"nombre": "Cerro Azul", "tarifa": 3000, "alias": []},
    {"nombre": "Entre Cerros", "tarifa": 3000, "alias": []},
    {"nombre": "Entre Bosques", "tarifa": 3000, "alias": []},
    {"nombre": "Laureles de Terranova", "tarifa": 3000, "alias": ["laureles"]},
    {"nombre": "Hacienda y Ceiba", "tarifa": 2000, "alias": ["hacienda", "la ceiba", "ceiba"]},
]
ITEMS = [{"product": "menu_del_dia", "qty": 1, "elecciones": {"Sopa": "Ajiaco"}}]


@pytest.mark.parametrize(
    "address, zone",
    [
        ("Cerro Azul torre 3 apto 402", "Cerro Azul"),
        ("cerro asul t3", "Cerro Azul"),  # error de tipeo
        ("CERRO CLARO", "Cerro Claro"),
        ("Entre cerros bloque 2", "Entre Cerros"),
        ("entre bosque casa 5", "Entre Bosques"),
        ("Laureles de Terranova apto 202", "Laureles de Terranova"),
        ("En la Ceiba casa 10", "Hacienda y Ceiba"),
        ("hacienda, al lado de la portería", "Hacienda y Ceiba"),
    ],
)
def test_informal_addresses_resolve_to_their_zone(address, zone):
    assert find_zone(address, ZONES)["nombre"] == zone


@pytest.mark.parametrize("address", ["Cra 59c #40-26", "Vereda El Salado finca 3", "cerro"])
def test_unknown_or_ambiguous_addresses_resolve_to_no_zone(address):
    assert find_zone(address, ZONES) is None


def test_two_zones_in_one_address_is_ambiguous():
    assert len(matching_zones("cerro azul o cerro claro", ZONES)) == 2
    assert find_zone("cerro azul o cerro claro", ZONES) is None


def _context(fake_store, **conversation_fields):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, delivery_zones=ZONES)
    seed_active_menu(fake_store, business)
    customer = seed_customer(fake_store, business, name="Ana")
    conversation = seed_conversation(fake_store, business, customer.customer_id, **conversation_fields)
    ctx = FakeToolContext(business.bussiness_id, customer.customer_id)
    ctx.state["conversation_id"] = conversation.conversation_id
    return business, customer, conversation, ctx


def test_known_zone_sets_the_fee_used_in_the_total(fake_store):
    _, _, conversation, ctx = _context(fake_store)

    resolved = resolve_delivery_fee("Hacienda casa 4", ctx)

    assert resolved == {"status": "zona", "zone": "Hacienda y Ceiba", "delivery_fee": 2000}
    assert _delivery_fee(conversation) == 2000
    assert conversation.delivery_status == DeliveryStatus.ZONA.value


def test_no_total_without_a_resolved_delivery_fee(fake_store):
    _, _, conversation, ctx = _context(fake_store)

    assert _delivery_fee(conversation) is None
    assert save_order({"items": ITEMS}, ctx)["order_id"] is None


def test_unknown_zone_clears_a_previous_fee(fake_store):
    """Una tarifa de un pedido anterior no puede colarse en el nuevo."""
    _, _, conversation, ctx = _context(
        fake_store, delivery_address="Cerro Azul", delivery_fee=3000,
        delivery_status=DeliveryStatus.ZONA.value,
    )

    resolved = resolve_delivery_fee("Vereda El Salado finca 3", ctx)

    assert resolved["status"] == "zona_desconocida"
    assert "Cerro Azul" in resolved["zonas"]
    assert _delivery_fee(conversation) is None


def test_the_order_stores_zone_fee_and_the_confirmed_total(fake_store):
    _, _, _, ctx = _context(fake_store)
    resolve_delivery_fee("Cerro Azul torre 3", ctx)

    result = save_order({"items": ITEMS, "address": "Cerro Azul torre 3"}, ctx)

    order = fake_store.stores[Orders][result["order_id"]]
    assert result["total"] == 21500
    assert (order.delivery_zone, order.delivery_fee, order.order_total) == ("Cerro Azul", 3000, 21500)


# --- Cotización humana -----------------------------------------------------


async def test_unmapped_address_waits_for_a_human_quote(fake_store):
    _, _, conversation, ctx = _context(fake_store)
    resolve_delivery_fee("Vereda El Salado finca 3", ctx)

    with patch.object(quotes.HandoffService, "notify_human", new=AsyncMock(return_value=True)) as notify:
        result = await request_delivery_quote("Vereda El Salado finca 3", ctx)

    assert result == {"requested": True, "admin_notified": True}
    assert conversation.delivery_status == DeliveryStatus.PENDIENTE_COTIZACION.value
    assert notify.await_args.kwargs["motivo"] == "cotizar_domicilio"
    assert "Vereda El Salado" in notify.await_args.kwargs["detalle"]
    # Mientras tanto no hay total ni pedido.
    assert resolve_delivery_fee("Vereda El Salado finca 3", ctx) == {"status": "cotizacion_pendiente"}
    assert _delivery_fee(conversation) is None


async def test_admin_quote_reaches_the_customer_and_unlocks_the_total(fake_store):
    business, customer, conversation, ctx = _context(
        fake_store, delivery_address="Vereda El Salado finca 3",
        delivery_status=DeliveryStatus.PENDIENTE_COTIZACION.value,
    )
    admin_ctx = FakeToolContext(business.bussiness_id)

    with patch.object(quotes, "WhatsAppClient") as client_cls, patch.object(
        quotes.shared_agent_runner(), "append_human_message", new=AsyncMock()
    ) as note:
        client_cls.for_business.return_value.send_message = AsyncMock(return_value=True)
        result = await set_delivery_quote(admin_ctx, customer.phone_number, 6000)

    assert result["ok"] is True and result["customer_notified"] is True
    sent_to, text = client_cls.for_business.return_value.send_message.await_args.args
    assert sent_to == customer.phone_number
    assert "Vereda El Salado" in text and "$6.000" in text
    note.assert_awaited_once()  # la IA del cliente sabe del valor cuando responda
    assert resolve_delivery_fee("Vereda El Salado finca 3", ctx) == {"status": "cotizado", "delivery_fee": 6000}
    assert _delivery_fee(conversation) == 6000


async def test_admin_quote_without_a_pending_request_is_rejected(fake_store):
    business, customer, _, _ = _context(fake_store)

    result = await set_delivery_quote(FakeToolContext(business.bussiness_id), customer.phone_number, 6000)

    assert result == {"ok": False, "reason": "sin_cotizacion_pendiente"}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("4000 573012841359", (4000, "573012841359")),
        ("4k 573012841359", (4000, "573012841359")),
        ("domicilio $4.000 para 3012841359", (4000, "573012841359")),
        ("573012841359 5 mil", (5000, "573012841359")),
        ("3000 para Sebastián G", (3000, "sebastián g")),
        ("3000 para CO.1780159176423859", (3000, "co.1780159176423859")),
        ("4000", (4000, "")),
        ("gratis para Camilo", (0, "camilo")),
        ("0 573012841359", (0, "573012841359")),
        ("+57 3012841359 6000", None),  # teléfono partido en dos: que lo vea la IA
        ("4,5k 573012841359", None),
        ("900000 573012841359", None),
        ("ventas de hoy", None),
    ],
)
def test_admin_quote_parser_takes_one_value_and_who(text, expected):
    assert quotes.parse_quote_command(text) == expected


def _admin_message(text):
    message = Messages()
    message.type, message.content, message.conversation_id = "text", text, "conv-admin"
    return message


async def test_admin_quote_fast_path_quotes_without_the_agent(fake_store):
    business, customer, conversation, _ = _context(
        fake_store, delivery_address="Vereda El Salado finca 3",
        delivery_status=DeliveryStatus.PENDIENTE_COTIZACION.value,
    )
    admin = seed_customer(fake_store, business, phone="573009999999")
    with patch.object(quotes, "WhatsAppClient") as client_cls, patch.object(
        quotes.shared_agent_runner(), "append_human_message", new=AsyncMock()
    ), patch.object(quotes, "OutboundMessageService") as outbound:
        client_cls.for_business.return_value.send_message = AsyncMock(return_value=True)
        outbound.return_value.send_and_persist = AsyncMock()
        handled = await quotes.maybe_handle_admin_quote(AsyncMock(), business, admin, _admin_message(f"4k {customer.phone_number}"))

    assert handled
    assert (conversation.delivery_status, conversation.delivery_fee) == (DeliveryStatus.COTIZADO.value, 4000)
    reply = outbound.return_value.send_and_persist.await_args.kwargs
    assert reply["to_phone"] == admin.phone_number and "Ana" in reply["text"] and "$4.000" in reply["text"]


async def test_admin_quote_fast_path_leaves_the_rest_to_the_agent(fake_store):
    business, customer, _, _ = _context(fake_store)  # sin cotización pendiente
    admin = seed_customer(fake_store, business, phone="573009999999")
    with patch.object(quotes, "set_quote", new=AsyncMock()) as set_quote:
        assert not await quotes.maybe_handle_admin_quote(AsyncMock(), business, admin, _admin_message(f"4000 {customer.phone_number}"))
        assert not await quotes.maybe_handle_admin_quote(AsyncMock(), business, admin, _admin_message("4000 573000000000"))
        assert not await quotes.maybe_handle_admin_quote(AsyncMock(), business, admin, _admin_message("ventas de hoy"))
    set_quote.assert_not_awaited()


@pytest.mark.parametrize("who", ["Ana", "ana", "CO.1780159176423859", ""])
async def test_admin_quote_by_name_user_code_or_alone(fake_store, who):
    business = seed_business(fake_store)
    customer = seed_customer(fake_store, business, name="Ana María", phone="CO.1780159176423859")
    conversation = seed_conversation(
        fake_store, business, customer.customer_id,
        delivery_address="Uniminuto", delivery_status=DeliveryStatus.PENDIENTE_COTIZACION.value,
    )
    admin = seed_customer(fake_store, business, phone="573009999999")
    with patch.object(quotes, "set_quote", new=AsyncMock(return_value=True)) as set_quote, patch.object(
        quotes, "OutboundMessageService"
    ) as outbound:
        outbound.return_value.send_and_persist = AsyncMock()
        assert await quotes.maybe_handle_admin_quote(AsyncMock(), business, admin, _admin_message(f"3000 para {who}"))
    _, quoted, quoted_conversation, fee = set_quote.await_args.args
    assert (quoted.customer_id, quoted_conversation.conversation_id, fee) == (customer.customer_id, conversation.conversation_id, 3000)


async def test_admin_quote_with_two_matches_goes_to_the_agent(fake_store):
    business = seed_business(fake_store)
    for phone in ("573011111111", "573022222222"):
        c = seed_customer(fake_store, business, name="Ana", phone=phone)
        seed_conversation(fake_store, business, c.customer_id, delivery_status=DeliveryStatus.PENDIENTE_COTIZACION.value)
    admin = seed_customer(fake_store, business, phone="573009999999")
    with patch.object(quotes, "set_quote", new=AsyncMock()) as set_quote:
        assert not await quotes.maybe_handle_admin_quote(AsyncMock(), business, admin, _admin_message("3000 Ana"))
        assert not await quotes.maybe_handle_admin_quote(AsyncMock(), business, admin, _admin_message("3000"))
        assert not await quotes.maybe_handle_admin_quote(AsyncMock(), business, admin, _admin_message("3000 Pedro"))
    set_quote.assert_not_awaited()
    result = await set_delivery_quote(FakeToolContext(business.bussiness_id), "Ana", 3000)
    assert result["reason"] == "cliente_no_identificado" and len(result["pendientes"]) == 2


def test_agents_fall_back_across_the_model_pool_on_429():
    from app.ai.ai_restaurant import admin_agent, courier_agent, customer_agent
    from app.ai.core.genai_client import FallbackGemini

    # Sin reintento sobre el mismo modelo: el 429 salta al siguiente del pool.
    assert all(
        isinstance(agent.model, FallbackGemini) and agent.model.retry_options is None
        for agent in (admin_agent, courier_agent, customer_agent)
    )


def test_admin_can_add_a_zone_so_it_resolves_on_its_own(fake_store):
    business, _, _, ctx = _context(fake_store)
    admin_ctx = FakeToolContext(business.bussiness_id)

    add_delivery_zone(admin_ctx, "Vereda El Salado", 6000, alias=["el salado"])
    add_delivery_zone(admin_ctx, "Cerro Azul", 3500)  # actualiza, no duplica

    zones = next(iter(fake_store.stores[Configurations].values())).delivery_zones
    assert [z["nombre"] for z in zones].count("Cerro Azul") == 1
    assert resolve_delivery_fee("finca 3, el salado", ctx)["delivery_fee"] == 6000
    assert resolve_delivery_fee("cerro azul t1", ctx)["delivery_fee"] == 3500


BELEN = [
    {"nombre": "Belén", "tarifa": 3000, "alias": []},
    {"nombre": "Belén Rincón", "tarifa": 5000, "alias": []},
]


def test_the_most_specific_zone_wins():
    assert find_zone("belen rincon", BELEN)["nombre"] == "Belén Rincón"
    assert find_zone("Calle 30 # 80-10, Belén", BELEN)["nombre"] == "Belén"
    # Ninguna contiene a la otra: no se adivina.
    assert find_zone("cerro azul o cerro claro", ZONES) is None


def test_a_remembered_zone_is_reused_for_the_same_address_with_todays_fee(fake_store):
    # Dirección que hoy no da una sola zona, pero esta conversación ya la tenía.
    _, _, conversation, ctx = _context(
        fake_store, delivery_address="cerro azul o cerro claro", delivery_zone="Cerro Azul",
        delivery_fee=2500, delivery_status=DeliveryStatus.ZONA.value,
    )

    resolved = resolve_delivery_fee("Cerro Azul o Cerro Claro", ctx)

    assert resolved == {"status": "zona", "zone": "Cerro Azul", "delivery_fee": 3000}


async def test_a_free_zone_confirms_with_zero_fee_and_says_gratis(fake_store):
    from app.ai.ai_restaurant.user.tools.order_tools import render_order_state, update_order
    from app.services.printer_client.print_jobs import build_ticket

    business, _, _, ctx = _context(fake_store)
    with patch("app.ai.ai_restaurant.user.tools.customer_tools.zones_for",
               return_value=[{"nombre": "Cerro Azul", "tarifa": 0, "alias": []}]):
        await update_order(
            [{"accion": "agregar", "producto": "menu_del_dia", "elecciones": {"Sopa": "Frijoles"}},
             {"accion": "entrega", "direccion": "Calle 10 # 20-30", "barrio": "Cerro Azul"},
             {"accion": "pago", "forma": "efectivo"}],
            ctx,
        )
    order = ctx.state["pedido"]
    assert order["domicilio"] == 0 and order["total"] == order["subtotal"]
    assert "domicilio: gratis" in render_order_state(ctx.state)
    assert "Domicilio: gratis" in build_ticket(business, None, Orders(order_id="ped0000001", delivery_fee=0, order_total=18500), "1x Menú")
