"""Lo que el panel interno puede y no puede guardar: dinero validado por el
backend (zonas, precios), campos obsoletos fuera de la API y un alta de
negocio que no deje al bot sin poder contestar."""

from fastapi.testclient import TestClient

from app.ai.ai_restaurant.admin.admin_tools import add_delivery_zone
from app.config import admin_api_settings
from app.firestore_db.firebase_schemas import Businesses, Configurations
from app.services import worker_entry
from tests.conftest import FakeToolContext, seed_business, seed_configuration

client = TestClient(worker_entry.app)
HEADERS = {"X-API-Key": admin_api_settings.API_KEY}


def _patch(business_id: str, body: dict):
    return client.patch(f"/api/v1/admin/configurations/{business_id}", json=body, headers=HEADERS)


def test_zones_that_would_charge_wrong_are_rejected(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    ok = {"nombre": "Cerro Azul", "tarifa": 3000, "alias": ["cerro asul"]}

    assert _patch(business.bussiness_id, {"delivery_zones": [ok]}).status_code == 200
    for bad in (
        [{**ok, "tarifa": -1}],
        [{**ok, "tarifa": 2500.5}],
        [{**ok, "alias": ["la"]}],
        [{**ok, "nombre": "  "}],
        # Mismo alias en dos zonas: find_zone no cobraría ninguna.
        [ok, {"nombre": "Cerro Claro", "tarifa": 2000, "alias": ["Cerro Asul"]}],
    ):
        assert _patch(business.bussiness_id, {"delivery_zones": bad}).status_code == 422, bad

    stored = next(iter(fake_store.stores[Configurations].values()))
    assert stored.delivery_zones == [ok]


def test_the_admin_chat_tool_follows_the_same_zone_rules(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, delivery_zones=[{"nombre": "Cerro Azul", "tarifa": 3000, "alias": []}])
    ctx = FakeToolContext(business.bussiness_id)

    assert add_delivery_zone(ctx, "Vereda", -500)["reason"] == "zona_invalida"
    assert add_delivery_zone(ctx, "Cerro Claro", 2000, alias=["cerro azul"])["reason"] == "zona_invalida"
    assert add_delivery_zone(ctx, "Vereda El Salado", 6000, alias=["el salado"])["ok"] is True


def test_obsolete_fields_are_neither_saved_nor_returned(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)

    response = _patch(
        business.bussiness_id,
        {"delivery_confirmation_enabled": True, "has_thermal_printer": True, "daily_menu_expected_ready_time": "10:30"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["daily_menu_expected_ready_time"] == "10:30"
    for obsolete in ("delivery_confirmation_enabled", "delivery_confirmation_interval_minutes",
                     "manager_notification_delay_minutes", "has_thermal_printer"):
        assert obsolete not in body
    stored = next(iter(fake_store.stores[Configurations].values()))
    assert not stored.delivery_confirmation_enabled and not stored.has_thermal_printer
    assert _patch(business.bussiness_id, {"daily_menu_expected_ready_time": "10h"}).status_code == 422


def test_prompt_fields_have_a_size_limit(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)

    assert _patch(business.bussiness_id, {"tono": "x" * 501}).status_code == 422
    assert _patch(business.bussiness_id, {"faq": [{"pregunta": "", "respuesta": "x"}]}).status_code == 422


def test_fixed_items_need_a_valid_price_and_an_existing_business(fake_store):
    business = seed_business(fake_store)
    url = "/api/v1/admin/restaurant/fixed-menu-items"
    item = {"business_id": business.bussiness_id, "nombre": "Limonada", "precio": 5000}

    created = client.post(url, json=item, headers=HEADERS)
    assert created.status_code == 201
    assert created.json()["catalog_retailer_id"] is None

    for bad in ({**item, "precio": -1}, {**item, "precio": None}, {**item, "tipo_precio": "gratis"}):
        assert client.post(url, json=bad, headers=HEADERS).status_code == 422, bad
    assert client.post(url, json={**item, "business_id": "no-existe"}, headers=HEADERS).status_code == 404


def _new_business(**overrides):
    body = {
        "name": "Nuevo",
        "legal_name": "Nuevo SAS",
        "bussiness_vertical": "restaurant",
        "phone": "3001112233",
        "whatsapp_phone_number": "+57 300 111 2233",
        "whatsapp_phone_number_id": "1344009488801289",
        **overrides,
    }
    return client.post("/api/v1/admin/businesses", json=body, headers=HEADERS)


def test_business_creation_rejects_what_would_leave_the_bot_mute(fake_store):
    created = _new_business()
    assert created.status_code == 201
    stored = fake_store.stores[Businesses][created.json()["bussiness_id"]]
    assert stored.whatsapp_phone_number == "573001112233"
    assert fake_store.stores[Configurations][stored.bussiness_id].business_id == stored.bussiness_id

    # Mismo número (escrito distinto) o mismo phone_number_id -> 409.
    assert _new_business(whatsapp_phone_number_id="999999").status_code == 409
    assert _new_business(whatsapp_phone_number="573009990000").status_code == 409
    # Sin phone_number_id el webhook descartaría sus eventos; dentist no tiene agente.
    assert _new_business(whatsapp_phone_number="573009990000", whatsapp_phone_number_id=None).status_code == 422
    assert _new_business(
        whatsapp_phone_number="573009990000", whatsapp_phone_number_id="555555", bussiness_vertical="dentist"
    ).status_code == 422


def test_the_panel_lists_businesses_even_old_incomplete_ones(fake_store):
    seed_business(fake_store, name="Zeta")
    seed_business(fake_store, name="alfa", legal_name=None, phone=None, whatsapp_phone_number=None)

    response = client.get("/api/v1/admin/businesses", headers=HEADERS)

    assert response.status_code == 200
    assert [b["name"] for b in response.json()] == ["alfa", "Zeta"]


def test_editing_a_business_cannot_touch_status_secrets_or_vertical(fake_store):
    business = seed_business(fake_store, kapso_api_key_secret="projects/p/secrets/x", webhook_tenant_key="k_1")
    other = seed_business(fake_store, whatsapp_phone_number="573005556677", whatsapp_phone_number_id="77777")
    url = f"/api/v1/admin/businesses/{business.bussiness_id}"

    response = client.patch(
        url,
        json={"name": "Edy Gourmet 2", "status": "inactive", "kapso_api_key_secret": "otro",
              "webhook_tenant_key": "robada", "bussiness_vertical": "dentist"},
        headers=HEADERS,
    )

    assert response.status_code == 200 and response.json()["name"] == "Edy Gourmet 2"
    stored = fake_store.stores[Businesses][business.bussiness_id]
    assert (stored.status, stored.kapso_api_key_secret, stored.webhook_tenant_key, stored.bussiness_vertical) == (
        "active", "projects/p/secrets/x", "k_1", "restaurant",
    )
    # El número de otro negocio -> 409; vaciar el phone_number_id -> 400.
    assert client.patch(url, json={"whatsapp_phone_number": other.whatsapp_phone_number}, headers=HEADERS).status_code == 409
    assert client.patch(url, json={"whatsapp_phone_number_id": None}, headers=HEADERS).status_code == 400
    # Su propio número no choca consigo mismo.
    assert client.patch(url, json={"whatsapp_phone_number": business.whatsapp_phone_number}, headers=HEADERS).status_code == 200


def test_couriers_are_normalized_cannot_be_admins_and_keep_their_date(fake_store):
    business = seed_business(fake_store)
    seed_configuration(
        fake_store, business, admin_phone_numbers=["573001234567"],
        couriers=[{"phone": "573011112222", "name": "Carlos", "added_at": "2026-10-01T10:00:00-05:00"}],
    )
    url = f"/api/v1/admin/configurations/{business.bussiness_id}/couriers"

    response = client.put(
        url, json={"couriers": [{"phone": "301 111 2222", "name": "Carlos R"}, {"phone": "3022223333", "name": "Ana"}]},
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["couriers"] == [{"phone": "573011112222", "name": "Carlos R"}, {"phone": "573022223333", "name": "Ana"}]
    stored = next(iter(fake_store.stores[Configurations].values())).couriers
    assert stored[0]["added_at"] == "2026-10-01T10:00:00-05:00" and stored[1]["added_at"]
    assert client.put(url, json={"couriers": [{"phone": "3001234567", "name": "Admin"}]}, headers=HEADERS).status_code == 400
    assert client.put(url, json={"couriers": [{"phone": "12", "name": "X"}]}, headers=HEADERS).status_code == 400


CUENTA = {"banco": "Bancolombia", "tipo": "ahorros", "numero": "12345678901", "titular": "Edy Gourmet SAS"}


def test_accepting_transfers_requires_an_account(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    bid = business.bussiness_id

    # metodos_pago vacío = las dos formas, así que también pide cuenta.
    assert _patch(bid, {"metodos_pago": []}).status_code == 422
    assert _patch(bid, {"metodos_pago": ["transferencia"]}).status_code == 422
    assert _patch(bid, {"metodos_pago": ["efectivo"]}).status_code == 200
    assert _patch(bid, {"cuentas_transferencia": [{**CUENTA, "numero": "12-34"}]}).status_code == 422
    assert _patch(bid, {"metodos_pago": ["efectivo", "transferencia"], "cuentas_transferencia": [CUENTA]}).status_code == 200
    # Quitar la última cuenta mientras se acepta transferencia, no.
    assert _patch(bid, {"cuentas_transferencia": []}).status_code == 422
    stored = next(iter(fake_store.stores[Configurations].values()))
    assert stored.cuentas_transferencia == [CUENTA]


def test_reminder_hour_is_validated(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)

    assert _patch(business.bussiness_id, {"recordatorio_menu_hora": "9:30"}).status_code == 422
    response = _patch(business.bussiness_id, {"recordatorio_menu_hora": "09:30"})
    assert response.status_code == 200 and response.json()["recordatorio_menu_hora"] == "09:30"
    assert _patch(business.bussiness_id, {"recordatorio_menu_hora": None}).json()["recordatorio_menu_hora"] is None
