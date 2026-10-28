from unittest import mock

import httpx

from app.ai.ai_restaurant.admin.admin_tools import sync_catalog
from app.ai.ai_restaurant.user.tools.customer_tools import save_order, validate_order_items
from app.firestore_db.firebase_schemas import FixedMenuItems, Orders
from app.schemas.enum_schemas import DeliveryStatus
from app.services.daily_menu.catalog_sync import apply_catalog, parse_price
from tests.conftest import (
    FakeToolContext,
    seed_active_menu,
    seed_business,
    seed_configuration,
    seed_conversation,
    seed_customer,
)

CATALOG_ID = "998877"


def _response(body: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("GET", "https://graph.facebook.com"))


def _seed_item(fake_store, business, retailer_id, nombre, precio, **overrides):
    item = FixedMenuItems()
    item.business_id = business.bussiness_id
    item.catalog_retailer_id = retailer_id
    item.nombre = nombre
    item.precio = precio
    item.tipo_precio = "fijo"
    item.is_active = True
    item.descripcion = None
    for key, value in overrides.items():
        setattr(item, key, value)
    return fake_store.seed(item)


def test_parse_price_formats_from_meta():
    assert parse_price("$25.000") == 25000
    assert parse_price("COP25,000.00") == 25000
    assert parse_price("$ 25.000,00") == 25000
    assert parse_price("") is None
    assert parse_price(None) is None


async def test_sync_creates_updates_and_deletes_only_catalog_items(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, whatsapp_catalog_id=CATALOG_ID)
    manual = _seed_item(fake_store, business, None, "Gaseosa manual", 3000)
    _seed_item(fake_store, business, "bandeja", "Bandeja paisa", 25000)
    _seed_item(fake_store, business, "sopa", "Sopa del día", 8000)

    pages = [
        _response(
            {
                "data": [{"retailer_id": "bandeja", "name": "Bandeja paisa", "price": "$27.000"}],
                "paging": {"next": "https://graph.facebook.com/next-page"},
            }
        ),
        _response(
            {
                "data": [
                    {"retailer_id": "limonada", "name": "Limonada", "price": "COP5,000.00"},
                    {"retailer_id": "postre", "name": "Postre", "price": "", "availability": "out of stock"},
                ]
            }
        ),
    ]
    with mock.patch(
        "app.services.whatsapp_client.whatsapp_client.WhatsAppClient.get", side_effect=pages
    ) as get:
        result = await sync_catalog(FakeToolContext(business.bussiness_id))

    assert result == {
        "ok": True,
        "creados": ["Limonada", "Postre"],
        "modificados": ["Bandeja paisa: precio $25.000 → $27.000"],
        "eliminados": ["Sopa del día"],
    }
    assert get.call_args_list[1].args[0] == "https://graph.facebook.com/next-page"

    items = {i.catalog_retailer_id: i for i in fake_store.stores[FixedMenuItems].values()}
    assert set(items) == {None, "bandeja", "limonada", "postre"}
    assert items[None] is manual
    assert items["postre"].tipo_precio == "no_legible" and items["postre"].is_active is False


def _order_context(fake_store, *, with_daily_menu: bool):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    if with_daily_menu:
        seed_active_menu(fake_store, business)
    customer = seed_customer(fake_store, business)
    conversation = seed_conversation(
        fake_store, business, customer.customer_id,
        delivery_fee=3000, delivery_status=DeliveryStatus.ZONA.value,
    )
    ctx = FakeToolContext(business.bussiness_id, customer.customer_id)
    ctx.state["conversation_id"] = conversation.conversation_id
    return business, ctx


def test_catalog_product_is_ordered_with_its_catalog_price(fake_store):
    business, ctx = _order_context(fake_store, with_daily_menu=True)
    item = _seed_item(fake_store, business, "limonada", "Limonada", 5000)
    items = [
        {"product": "menu_del_dia", "qty": 1, "elecciones": {"Sopa": "Ajiaco"}},
        # unit_price inventado por el modelo: se ignora.
        {"product": f"fixed:{item.fixed_item_id}", "qty": 2, "unit_price": 1},
    ]

    result = save_order({"items": items}, ctx)

    assert result["total"] == 18500 + 2 * 5000 + 3000
    orders = {o.menu_id: o for o in fake_store.stores[Orders].values()}
    assert orders[f"fixed:{item.fixed_item_id}"].item_name == "Limonada"


def test_catalog_product_can_be_ordered_without_daily_menu(fake_store):
    business, ctx = _order_context(fake_store, with_daily_menu=False)
    item = _seed_item(fake_store, business, "limonada", "Limonada", 5000)

    ok = validate_order_items([{"product": f"fixed:{item.fixed_item_id}", "qty": 1}], ctx)
    no_menu = validate_order_items([{"product": "menu_del_dia", "qty": 1}], ctx)

    assert ok["valid"] is True and ok["resolved_items"][0]["unit_price"] == 5000
    assert no_menu["valid"] is False


def test_out_of_stock_or_unpriced_catalog_products_cannot_be_ordered(fake_store):
    business, ctx = _order_context(fake_store, with_daily_menu=True)
    agotado = _seed_item(fake_store, business, "postre", "Postre", 6000, is_active=False)
    sin_precio = _seed_item(fake_store, business, "jugo", "Jugo", None, tipo_precio="no_legible")

    result = validate_order_items(
        [
            {"product": f"fixed:{agotado.fixed_item_id}", "qty": 1},
            {"product": f"fixed:{sin_precio.fixed_item_id}", "qty": 1},
        ],
        ctx,
    )

    assert result["valid"] is False
    assert result["resolved_items"] == []
    assert len(result["errors"]) == 2


async def test_sync_does_not_touch_firestore_when_meta_fails(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, whatsapp_catalog_id=CATALOG_ID)
    _seed_item(fake_store, business, "bandeja", "Bandeja paisa", 25000)

    with mock.patch(
        "app.services.whatsapp_client.whatsapp_client.WhatsAppClient.get",
        return_value=_response({"error": {"message": "permiso"}}, status=403),
    ):
        result = await sync_catalog(FakeToolContext(business.bussiness_id))

    assert result == {"ok": False, "reason": "error_leyendo_catalogo"}
    assert len(fake_store.stores[FixedMenuItems]) == 1


async def test_sync_without_catalog_configured(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)

    result = await sync_catalog(FakeToolContext(business.bussiness_id))

    assert result == {"ok": False, "reason": "catalogo_no_configurado"}


def test_carta_and_meta_catalog_do_not_delete_each_other(fake_store):
    business = seed_business(fake_store)
    _seed_item(fake_store, business, "meta-1", "Hamburguesa", 20000)

    result = apply_catalog(
        business.bussiness_id,
        [{"retailer_id": "carta:bebidas:agua", "name": "Agua", "price": "5000", "categoria": "Bebidas"}],
        True,
    )
    assert result == {"creados": ["Agua"], "modificados": [], "eliminados": []}

    # Meta no trae el producto de la carta: no debe borrarlo.
    apply_catalog(business.bussiness_id, [{"retailer_id": "meta-1", "name": "Hamburguesa", "price": "20000"}])
    items = {i.nombre: i for i in fake_store.stores[FixedMenuItems].values()}
    assert set(items) == {"Hamburguesa", "Agua"}
    assert items["Agua"].categoria == "Bebidas" and items["Agua"].precio == 5000
