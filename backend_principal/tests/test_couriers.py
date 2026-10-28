"""Domiciliarios administrados desde el chat admin."""

from app.ai.ai_restaurant.admin.admin_tools import add_courier, list_couriers, remove_courier
from tests.conftest import FakeToolContext, seed_business, seed_configuration


def test_admin_adds_lists_and_removes_couriers(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    ctx = FakeToolContext(business.bussiness_id)

    add_courier(ctx, "300 111 2233", "Carlos")
    add_courier(ctx, "+57 3004445566", "Luis")
    add_courier(ctx, "3001112233", "Carlos Pérez")  # mismo número: renombra, no duplica

    assert list_couriers(ctx)["domiciliarios"] == [
        {"nombre": "Luis", "telefono": "573004445566"},
        {"nombre": "Carlos Pérez", "telefono": "573001112233"},
    ]

    result = remove_courier(ctx, "573004445566")
    assert result["eliminado"] == "Luis"
    assert list_couriers(ctx)["domiciliarios"] == [{"nombre": "Carlos Pérez", "telefono": "573001112233"}]
    assert remove_courier(ctx, "3009999999") == {"ok": False, "reason": "domiciliario_no_encontrado"}


def test_invalid_phone_is_rejected(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)

    assert add_courier(FakeToolContext(business.bussiness_id), "123", "X") == {
        "ok": False, "reason": "telefono_invalido",
    }
