"""Reglas del negocio en Firestore: lo que el admin configura llega a la
instrucción del agente y a las validaciones del pedido, sin tocar código."""

from fastapi.testclient import TestClient

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


def _ctx(fake_store, **config):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, restaurant_name="Donde Edy", **config)
    seed_active_menu(fake_store, business)
    customer = seed_customer(fake_store, business)
    conversation = seed_conversation(fake_store, business, customer.customer_id)
    return business, FakeToolContext(business.bussiness_id, customer.customer_id, conversation.conversation_id)


def test_the_profile_reaches_the_instruction(fake_store):
    _, ctx = _ctx(
        fake_store,
        tono="cercano, con emojis",
        metodos_pago=["efectivo"],
        faq=[{"pregunta": "¿Tienen parqueadero?", "respuesta": "Sí, frente al local."}],
    )

    block = render_state(ctx.state)

    assert "Negocio: Donde Edy" in block
    assert "Tono con los clientes: cercano, con emojis" in block
    assert "Formas de pago aceptadas: efectivo" in block
    assert "- ¿Tienen parqueadero?: Sí, frente al local." in block


async def test_payment_methods_and_minimum_come_from_the_configuration(fake_store):
    _, ctx = _ctx(fake_store, metodos_pago=["efectivo"], pedido_minimo=30000, delivery_zones=[{"nombre": "Cerro Azul", "tarifa": 3000, "alias": []}])

    result = await update_order(
        [{"accion": "agregar", "producto": "menu_del_dia", "elecciones": {"Sopa": "Ajiaco"}},
         {"accion": "entrega", "direccion": "Calle 10 # 20-30", "barrio": "Cerro Azul"},
         {"accion": "pago", "forma": "transferencia"}],
        ctx,
    )
    assert "Forma de pago no válida: solo efectivo, acuerdo." in result["errores"]

    await update_order([{"accion": "pago", "forma": "efectivo"}], ctx)
    assert (await confirm_order(ctx))["errores"] == ["El pedido mínimo es $30.000 sin domicilio."]


def test_editing_the_configuration_refreshes_the_cache(fake_store):
    business, ctx = _ctx(fake_store)
    assert tenant_profile.get_profile(business.bussiness_id)["tono"] is None

    response = TestClient(worker_entry.app).patch(
        f"/api/v1/admin/configurations/{business.bussiness_id}",
        json={
            "tono": "formal",
            "metodos_pago": ["transferencia"],
            "cuentas_transferencia": [{"banco": "Nequi", "tipo": "billetera", "numero": "3001234567", "titular": "Edy"}],
        },
        headers={"X-API-Key": admin_api_settings.API_KEY},
    )

    assert response.status_code == 200
    assert tenant_profile.get_profile(business.bussiness_id)["tono"] == "formal"
    assert tenant_profile.get_profile(business.bussiness_id)["metodos_pago"] == ["transferencia"]
