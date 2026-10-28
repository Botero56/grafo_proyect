"""Dos negocios con configuraciones distintas no se mezclan: cada uno ve su
tono, horario, zonas, formas de pago, mínimo y cuentas, y editar uno desde
el panel no toca al otro."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.ai.ai_restaurant.user.tools.customer_tools import resolve_delivery_fee
from app.ai.ai_restaurant.user.tools.order_tools import confirm_order, render_state, update_order
from app.config import admin_api_settings
from app.services import tenant_profile, worker_entry
from tests.conftest import (
    FakeToolContext,
    seed_active_menu,
    seed_business,
    seed_configuration,
    seed_conversation,
    seed_customer,
)

client = TestClient(worker_entry.app)
HEADERS = {"X-API-Key": admin_api_settings.API_KEY}
TODAY = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Bogota")).date().isoformat()

CUENTA_A = {"banco": "Bancolombia", "tipo": "ahorros", "numero": "11111111111", "titular": "Donde Edy SAS"}
CUENTA_B = {"banco": "Nequi", "tipo": "billetera", "numero": "3002222222", "titular": "La Ceiba"}
MENU = {"accion": "agregar", "producto": "menu_del_dia", "elecciones": {"Sopa": "Ajiaco"}}


def _tenant(fake_store, phone: str, **config):
    business = seed_business(fake_store, whatsapp_phone_number=phone, whatsapp_phone_number_id=phone[-6:])
    seed_configuration(fake_store, business, **config)
    seed_active_menu(fake_store, business)
    customer = seed_customer(fake_store, business)
    conversation = seed_conversation(fake_store, business, customer.customer_id)
    return business, FakeToolContext(business.bussiness_id, customer.customer_id, conversation.conversation_id)


def _two_tenants(fake_store):
    a = _tenant(
        fake_store, "573001110000",
        restaurant_name="Donde Edy", tono="formal, sin emojis",
        metodos_pago=["transferencia"], cuentas_transferencia=[CUENTA_A], pedido_minimo=30000,
        delivery_zones=[{"nombre": "Cerro Azul", "tarifa": 3000, "alias": []}],
        # Hoy no abre.
        opening_hours={"timezone": "America/Bogota", "weekly": {}, "exceptions": [{"date": TODAY, "closed": True}], "enforce": True},
    )
    b = _tenant(
        fake_store, "573002220000",
        restaurant_name="La Ceiba", tono="cercano, con emojis",
        metodos_pago=["efectivo", "transferencia"], cuentas_transferencia=[CUENTA_B],
        delivery_zones=[{"nombre": "Cerro Azul", "tarifa": 5000, "alias": []}],
    )
    return a, b


def test_each_business_gets_only_its_own_profile(fake_store):
    (_, ctx_a), (_, ctx_b) = _two_tenants(fake_store)

    block_a, block_b = render_state(ctx_a.state), render_state(ctx_b.state)

    assert "Negocio: Donde Edy" in block_a and "formal, sin emojis" in block_a and "Pedido mínimo" in block_a
    assert "Ahora: CERRADO" in block_a
    assert "Negocio: La Ceiba" in block_b and "cercano, con emojis" in block_b
    assert "Donde Edy" not in block_b and "Pedido mínimo" not in block_b and "Horario" not in block_b


def test_the_same_address_costs_what_each_business_charges(fake_store):
    (_, ctx_a), (_, ctx_b) = _two_tenants(fake_store)

    assert resolve_delivery_fee("Cerro Azul torre 3", ctx_a)["delivery_fee"] == 3000
    assert resolve_delivery_fee("Cerro Azul torre 3", ctx_b)["delivery_fee"] == 5000


async def test_payment_hours_minimum_and_account_come_from_each_business(fake_store):
    (_, ctx_a), (_, ctx_b) = _two_tenants(fake_store)
    order = [MENU, {"accion": "entrega", "direccion": "Calle 10 # 20-30, Cerro Azul torre 3", "barrio": "Cerro Azul"}]

    # A no recibe efectivo; B sí.
    a = await update_order([*order, {"accion": "pago", "forma": "efectivo"}], ctx_a)
    assert any("Forma de pago no válida" in e for e in a["errores"])
    # A: el menú del día ($18.500) no llega a su mínimo de $30.000.
    await update_order([{"accion": "pago", "forma": "transferencia"}], ctx_a)
    assert (await confirm_order(ctx_a))["errores"] == ["El pedido mínimo es $30.000 sin domicilio."]
    # Con el mínimo cumplido, A sigue sin confirmar: hoy está cerrado.
    await update_order([{**MENU, "cantidad": 2}], ctx_a)
    assert "cerrado" in (await confirm_order(ctx_a))["errores"][0]

    await update_order([*order, {"accion": "pago", "forma": "transferencia"}], ctx_b)
    confirmed = await confirm_order(ctx_b)
    assert confirmed["ok"] is True
    assert confirmed["cuentas_transferencia"] == [CUENTA_B]


def test_editing_one_business_from_the_panel_leaves_the_other_alone(fake_store):
    (business_a, ctx_a), (business_b, ctx_b) = _two_tenants(fake_store)
    before_b = render_state(ctx_b.state)
    render_state(ctx_a.state)  # deja a A en caché

    response = client.patch(
        f"/api/v1/admin/configurations/{business_a.bussiness_id}", json={"tono": "muy breve"}, headers=HEADERS
    )

    assert response.status_code == 200
    assert tenant_profile.get_profile(business_a.bussiness_id)["tono"] == "muy breve"
    assert render_state(ctx_b.state) == before_b
    assert client.get(f"/api/v1/admin/configurations/{business_b.bussiness_id}", headers=HEADERS).json()["tono"] == "cercano, con emojis"
