"""Stock del día: la IA del admin solo pide el cambio, el código lo ubica y
lo aplica con el clic del admin; los pedidos se validan y descuentan en
código, nunca con lo que diga la IA."""

from unittest.mock import AsyncMock, patch

from app.ai.ai_restaurant.admin.admin_tools import request_stock_change
from app.ai.ai_restaurant.user.tools.customer_tools import get_active_menu, save_order, validate_order_items
from app.firestore_db.firebase_schemas import Orders
from app.services.daily_menu import stock
from app.services.daily_menu.orchestrator import MenuIngestionOrchestrator
from tests.conftest import (
    FakeToolContext,
    seed_active_menu,
    seed_business,
    seed_configuration,
    seed_conversation,
    seed_customer,
)
from tests.test_daily_menu_orchestrator import FakeWhatsAppClient, _button_message

PRODUCTS = {"pollo": "Pollo", "hamburguesa doble": "Hamburguesa doble", "menu del dia": "Ajiaco", "fresas": "Fresas"}
SOPA = {"Sopa": "Ajiaco"}


def test_match_product_by_name_words_or_typo():
    assert stock.match_product("se acabó el pollo", PRODUCTS) == ["Pollo"]
    assert stock.match_product("hamburguesa", PRODUCTS) == ["Hamburguesa doble"]
    assert stock.match_product("hamburgesa doble", PRODUCTS) == ["Hamburguesa doble"]
    assert stock.match_product("menú del día", PRODUCTS) == ["Ajiaco"]
    assert stock.match_product("res", PRODUCTS) == []  # no matchea dentro de "fresas"


def _context(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, delivery_zones=[{"nombre": "Centro", "tarifa": 3000, "alias": []}])
    seed_active_menu(fake_store, business)
    admin = seed_customer(fake_store, business, "573001111111", name="Admin")
    customer = seed_customer(fake_store, business, name="Ana")
    conversation = seed_conversation(
        fake_store, business, customer.customer_id, delivery_fee=3000, delivery_zone="Centro", delivery_status="zona"
    )
    ctx = FakeToolContext(business.bussiness_id, customer.customer_id, conversation.conversation_id)
    return business, admin, ctx


async def _admin_says(fake_store, business, admin, producto, **kwargs):
    """La IA del admin pide el cambio y el admin toca el primer botón."""
    client = FakeWhatsAppClient()
    with patch(
        "app.ai.ai_restaurant.admin.admin_tools.WhatsAppClient.for_business", return_value=client
    ):
        result = await request_stock_change(
            FakeToolContext(business.bussiness_id, admin.customer_id), producto, **kwargs
        )
    if result["ok"]:
        await MenuIngestionOrchestrator(client).maybe_handle(
            business, admin, _button_message(fake_store, business, admin, client.sent_buttons[-1][2][0]["id"])
        )
    return result, client


async def test_nothing_changes_until_the_admin_confirms(fake_store):
    business, admin, ctx = _context(fake_store)
    client = FakeWhatsAppClient()
    with patch("app.ai.ai_restaurant.admin.admin_tools.WhatsAppClient.for_business", return_value=client):
        result = await request_stock_change(
            FakeToolContext(business.bussiness_id, admin.customer_id), "pollo", agotado=True
        )

    assert result == {"ok": True, "producto": "Pollo"}
    assert "agotado" in client.sent_buttons[-1][1]
    assert stock.today_stock(business.bussiness_id) == {}

    # "No" tampoco cambia nada.
    await MenuIngestionOrchestrator(client).maybe_handle(
        business, admin, _button_message(fake_store, business, admin, client.sent_buttons[-1][2][1]["id"])
    )
    assert stock.today_stock(business.bussiness_id) == {}


async def test_sold_out_protein_is_rejected_and_shown_to_the_customer_ai(fake_store):
    business, admin, ctx = _context(fake_store)
    await _admin_says(fake_store, business, admin, "pollo", agotado=True)

    assert get_active_menu(ctx)["stock_hoy"] == {"Pollo": 0}
    result = validate_order_items(
        [{"product": "otras_opciones", "qty": 1, "elecciones": SOPA, "proteina": "pollo"}], ctx
    )
    assert result["valid"] is False
    assert "'Pollo' se agotó por hoy." in result["errors"]


async def test_remaining_units_are_discounted_by_confirmed_orders(fake_store):
    business, admin, ctx = _context(fake_store)
    _, client = await _admin_says(fake_store, business, admin, "menú del día", cantidad=2)
    assert "quedan *2*" in client.sent_messages[-1][1]

    menu_line = {"product": "menu_del_dia", "qty": 1, "elecciones": SOPA}
    # Dos líneas del mismo producto suman: 3 > 2.
    assert save_order({"items": [menu_line, {**menu_line, "qty": 2}]}, ctx)["status"] == "error"
    assert not fake_store.stores[Orders]

    assert save_order({"items": [{**menu_line, "qty": 2}]}, ctx)["order_id"]
    result = save_order({"items": [menu_line]}, ctx)
    assert result["status"] == "error"
    assert "se agotó por hoy" in result["errors"][0]


def test_ambiguous_product_is_not_guessed():
    products = {**PRODUCTS, "hamburguesa sencilla": "Hamburguesa sencilla"}
    assert stock.match_product("hamburguesa", products) == ["Hamburguesa doble", "Hamburguesa sencilla"]


async def test_unknown_product_sends_no_confirmation(fake_store):
    business, admin, _ = _context(fake_store)

    result, client = await _admin_says(fake_store, business, admin, "pizza", agotado=True)

    assert result["reason"] == "no_encontrado" and "Pollo" in result["productos_hoy"]
    assert not client.sent_buttons


async def test_back_without_limit(fake_store):
    business, admin, ctx = _context(fake_store)
    await _admin_says(fake_store, business, admin, "pollo", agotado=True)
    await _admin_says(fake_store, business, admin, "pollo")

    assert "stock_hoy" not in get_active_menu(ctx)


def test_a_button_from_another_day_changes_nothing(fake_store):
    business = seed_business(fake_store)
    reply = stock.apply_button(business.bussiness_id, "stock:2000-01-01:0:Pollo")

    assert "otro día" in reply
    assert stock.today_stock(business.bussiness_id) == {}
