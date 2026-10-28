"""Carrito del catálogo de WhatsApp (mensaje `order`): el código traduce cada
producto (nunca la IA), se cobra el precio que vio el cliente y lo que no
reconoce se le pregunta al admin una sola vez."""

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.sessions import InMemorySessionService

from app.ai.ai_restaurant.admin.admin_tools import resolve_catalog_code
from app.ai.ai_restaurant.user.tools import customer_tools, order_tools
from app.ai.core.agent_runner import AgentRunnerService, _daily_session_id
from app.firestore_db.firebase_schemas import CatalogMappings, FixedMenuItems, Messages
from app.services.daily_menu import catalog_orders
from app.services.daily_menu.catalog_orders import CatalogOrderHandler, mapping_id
from app.services.firestore_services.firestore_webhook_service import CART_PLACEHOLDER, WhatsAppWebhookService
from tests.conftest import FakeToolContext, seed_business, seed_configuration, seed_conversation, seed_customer
from tests.fakes import FakeFirestore

CATALOG = "3637339149808520"
CODE = "mks25c5cms"
PAYLOAD = json.loads((Path(__file__).parent / "fixtures" / "kapso_order_webhook.json").read_text(encoding="utf-8"))


@pytest.fixture
def runner():
    service = AgentRunnerService()
    service._session_service = InMemorySessionService()
    with patch.object(catalog_orders, "shared_agent_runner", return_value=service):
        yield service


@pytest.fixture
def sent():
    """(mensajes al cliente, avisos al admin)."""
    client = MagicMock()
    client.send_message = AsyncMock(return_value=True)
    with patch.object(catalog_orders.WhatsAppClient, "for_business", return_value=client), patch.object(
        catalog_orders.HandoffService, "notify_admins", new=AsyncMock(return_value=True)
    ) as notify:
        yield client.send_message, notify


def _business(store):
    business = seed_business(store)
    seed_configuration(store, business, whatsapp_catalog_id=CATALOG)
    return business


def _customer(store, business, phone="573001234567"):
    customer = seed_customer(store, business, phone=phone, name="Ana")
    return customer, seed_conversation(store, business, customer.customer_id)


def _fixed(store, business, nombre="Hamburguesa Tradicional", precio=18000, **fields):
    item = FixedMenuItems()
    item.business_id = business.bussiness_id
    item.nombre, item.precio, item.tipo_precio = nombre, precio, "fijo"
    item.is_active, item.categoria = True, "Catálogo"
    for key, value in fields.items():
        setattr(item, key, value)
    return store.seed(item)


def _mapping(store, business, producto, status="confirmed"):
    mapping = CatalogMappings()
    mapping.catalog_mapping_id = mapping_id(business.bussiness_id, CATALOG, CODE)
    mapping.business_id, mapping.catalog_id, mapping.product_retailer_id = business.bussiness_id, CATALOG, CODE
    mapping.producto, mapping.status, mapping.esperando = producto, status, []
    return store.seed(mapping)


def _message(store, conversation, wamid="wamid.A"):
    message = Messages()
    message.conversation_id = conversation.conversation_id
    message.whatsapp_message_id = wamid
    message.content = CART_PLACEHOLDER
    return store.seed(message)


def _cart(*items, catalog=CATALOG):
    return {
        "text": "",
        "catalog_id": catalog,
        "product_items": [
            {"product_retailer_id": code, "quantity": qty, "item_price": price, "currency": "COP"}
            for code, qty, price in items
        ],
    }


async def _handle(store, business, customer, conversation, cart, wamid="wamid.A"):
    message = _message(store, conversation, wamid)
    handled = await CatalogOrderHandler().maybe_handle(business, customer, conversation, message, cart)
    return handled, message


async def _pedido(runner, business, customer, conversation):
    session = await runner._session_service.get_session(
        app_name=f"j2ys-{business.bussiness_id}",
        user_id=customer.customer_id,
        session_id=_daily_session_id(conversation.conversation_id),
    )
    return session.state.get("pedido") if session else None


async def test_synced_code_loads_the_order_and_the_agent_gets_names_not_codes(fake_store, runner, sent):
    business = _business(fake_store)
    customer, conversation = _customer(fake_store, business)
    _fixed(fake_store, business, catalog_retailer_id=CODE)

    handled, message = await _handle(fake_store, business, customer, conversation, _cart((CODE, 2, 18000)))

    assert handled is False  # sigue al agente con el resumen
    pedido = await _pedido(runner, business, customer, conversation)
    assert [(i["name"], i["qty"], i["subtotal"]) for i in pedido["items"]] == [("Hamburguesa Tradicional", 2, 36000)]
    assert "Hamburguesa Tradicional" in message.content and CODE not in message.content
    sent[1].assert_not_awaited()


async def test_a_confirmed_mapping_resolves_the_code(fake_store, runner, sent):
    business = _business(fake_store)
    customer, conversation = _customer(fake_store, business)
    item = _fixed(fake_store, business)
    _mapping(fake_store, business, f"fixed:{item.fixed_item_id}")

    handled, _ = await _handle(fake_store, business, customer, conversation, _cart((CODE, 1, 18000)))

    assert handled is False
    assert [i["name"] for i in (await _pedido(runner, business, customer, conversation))["items"]] == ["Hamburguesa Tradicional"]


async def test_unknown_code_waits_for_the_admin_and_then_completes_the_order(fake_store, runner, sent):
    business = _business(fake_store)
    customer, conversation = _customer(fake_store, business)
    _fixed(fake_store, business)
    to_customer, to_admin = sent

    handled, _ = await _handle(fake_store, business, customer, conversation, _cart((CODE, 1, 18000)))

    assert handled is True
    assert to_customer.await_args.args[1] == catalog_orders.CUSTOMER_WAIT
    notice = to_admin.await_args.args[1]
    assert CODE in notice and "Hamburguesa Tradicional" in notice
    pedido = await _pedido(runner, business, customer, conversation)
    assert pedido["items"] == [] and len(pedido["carrito_pendiente"]) == 1
    assert any("identificando" in m for m in order_tools._missing(pedido))  # confirm_order no pasa

    admin = FakeToolContext(business.bussiness_id, "admin-1")
    result = await resolve_catalog_code(admin, "hamburguesa")

    assert result == {"ok": True, "codigo": CODE, "producto": "Hamburguesa Tradicional", "clientes_avisados": 1}
    pedido = await _pedido(runner, business, customer, conversation)
    assert [(i["name"], i["subtotal"]) for i in pedido["items"]] == [("Hamburguesa Tradicional", 18000)]
    assert pedido["carrito_pendiente"] == []
    mapping = fake_store.stores[CatalogMappings][mapping_id(business.bussiness_id, CATALOG, CODE)]
    assert (mapping.status, mapping.confirmed_by, mapping.esperando) == ("confirmed", "admin-1", [])
    assert "Ya confirmamos tu pedido" in to_customer.await_args.args[1]


async def test_admin_can_say_it_is_not_on_the_menu(fake_store, runner, sent):
    business = _business(fake_store)
    customer, conversation = _customer(fake_store, business)
    await _handle(fake_store, business, customer, conversation, _cart((CODE, 1, 18000)))

    result = await resolve_catalog_code(FakeToolContext(business.bussiness_id, "admin-1"), "no_es_del_menu")

    assert result["ok"] and result["producto"] == "no es del menú"
    assert catalog_orders.UNAVAILABLE in sent[0].await_args.args[1]
    assert (await _pedido(runner, business, customer, conversation))["carrito_pendiente"] == []


async def test_mixed_cart_loads_what_it_knows_and_waits_for_the_rest(fake_store, runner, sent):
    business = _business(fake_store)
    customer, conversation = _customer(fake_store, business)
    _fixed(fake_store, business, nombre="Limonada", precio=5000, catalog_retailer_id="limo1")

    handled, _ = await _handle(fake_store, business, customer, conversation, _cart(("limo1", 1, 5000), (CODE, 1, 18000)))

    assert handled is True
    pedido = await _pedido(runner, business, customer, conversation)
    assert [i["name"] for i in pedido["items"]] == ["Limonada"]
    assert [p["codigo"] for p in pedido["carrito_pendiente"]] == [CODE]


async def test_cart_price_is_charged_and_a_difference_is_reported(fake_store, runner, sent):
    business = _business(fake_store)
    customer, conversation = _customer(fake_store, business)
    _fixed(fake_store, business, precio=20000, catalog_retailer_id=CODE)

    await _handle(fake_store, business, customer, conversation, _cart((CODE, 1, 18000)))

    pedido = await _pedido(runner, business, customer, conversation)
    assert pedido["items"][0]["subtotal"] == 18000
    # save_order (en confirm_order) revalida la línea y conserva el precio del carrito.
    ctx = FakeToolContext(business.bussiness_id, customer.customer_id, conversation.conversation_id)
    revalidated = customer_tools.validate_order_items([order_tools._line(pedido["items"][0])], ctx)
    assert revalidated["resolved_items"][0]["unit_price"] == 18000
    assert "se le cobra el del carrito" in sent[1].await_args.args[1].lower()


async def test_update_order_cannot_set_the_cart_price(fake_store):
    business = _business(fake_store)
    customer, conversation = _customer(fake_store, business)
    item = _fixed(fake_store, business)
    ctx = FakeToolContext(business.bussiness_id, customer.customer_id, conversation.conversation_id)

    await order_tools.update_order([{"accion": "agregar", "producto": f"fixed:{item.fixed_item_id}", "precio_carrito": 1}], ctx)

    assert ctx.state["pedido"]["items"][0]["subtotal"] == 18000


async def test_a_cart_from_another_catalog_is_not_processed(fake_store, runner, sent):
    business = _business(fake_store)
    customer, conversation = _customer(fake_store, business)
    _fixed(fake_store, business, catalog_retailer_id=CODE)

    handled, _ = await _handle(fake_store, business, customer, conversation, _cart((CODE, 1, 18000), catalog="999"))

    assert handled is True
    assert sent[0].await_args.args[1] == catalog_orders.WRONG_CATALOG
    assert await _pedido(runner, business, customer, conversation) is None
    assert fake_store.stores[CatalogMappings] == {}


async def test_the_same_wamid_twice_loads_the_cart_once(fake_store, runner, sent):
    business = _business(fake_store)
    customer, conversation = _customer(fake_store, business)
    _fixed(fake_store, business, catalog_retailer_id=CODE)
    message = _message(fake_store, conversation)

    first = await CatalogOrderHandler().maybe_handle(business, customer, conversation, message, _cart((CODE, 1, 18000)))
    second = await CatalogOrderHandler().maybe_handle(business, customer, conversation, message, _cart((CODE, 1, 18000)))

    assert (first, second) == (False, False)  # el reintento deja que el flush conteste el resumen
    assert len((await _pedido(runner, business, customer, conversation))["items"]) == 1


async def test_a_day_menu_mapping_is_left_for_the_agent_to_complete(fake_store, runner, sent):
    business = _business(fake_store)
    customer, conversation = _customer(fake_store, business)
    _mapping(fake_store, business, "menu_del_dia")

    handled, message = await _handle(fake_store, business, customer, conversation, _cart((CODE, 1, 18500)))

    assert handled is False
    pedido = await _pedido(runner, business, customer, conversation)
    assert pedido["items"] == [] and pedido["por_elegir"] == [{"producto": "menu_del_dia", "cantidad": 1}]
    assert "Menú del día" in message.content and "update_order" in message.content


async def test_two_carts_with_the_same_unknown_code_ask_the_admin_once(fake_store, runner, sent):
    business = _business(fake_store)
    first = _customer(fake_store, business)
    second = _customer(fake_store, business, phone="573007654321")

    await asyncio.gather(
        _handle(fake_store, business, *first, _cart((CODE, 1, 18000)), wamid="wamid.1"),
        _handle(fake_store, business, *second, _cart((CODE, 2, 18000)), wamid="wamid.2"),
    )

    assert sent[1].await_count == 1
    mapping = fake_store.stores[CatalogMappings][mapping_id(business.bussiness_id, CATALOG, CODE)]
    assert sorted(mapping.esperando) == sorted([first[1].conversation_id, second[1].conversation_id])


def test_simultaneous_registrations_create_the_code_once():
    """Dos procesos que leen a la vez: la transacción deja uno solo como creador."""
    with FakeFirestore(snapshots=True) as store:
        entry = {"codigo": CODE, "cantidad": 1, "precio": 18000, "moneda": "COP"}
        results = []
        threads = [
            threading.Thread(target=lambda c=c: results.append(catalog_orders.register_unknown("biz", CATALOG, entry, c)))
            for c in ("conv-1", "conv-2")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(results) == [False, True]
        assert sorted(store.stores[CatalogMappings][mapping_id("biz", CATALOG, CODE)].esperando) == ["conv-1", "conv-2"]


async def test_order_webhook_reaches_the_draft(fake_store, runner, sent):
    business = _business(fake_store)
    _fixed(fake_store, business, catalog_retailer_id=CODE)

    result = WhatsAppWebhookService().process_incoming_payload(PAYLOAD)

    assert result["message"].content == CART_PLACEHOLDER and result["order"]["catalog_id"] == CATALOG
    handled = await CatalogOrderHandler().maybe_handle(
        result["business"], result["customer"], result["conversation"], result["message"], result["order"]
    )
    assert handled is False
    pedido = await _pedido(runner, business, result["customer"], result["conversation"])
    assert pedido["items"][0]["name"] == "Hamburguesa Tradicional"
