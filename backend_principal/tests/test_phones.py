"""Teléfonos de admin normalizados al guardar y al comparar (A4)."""

from fastapi.testclient import TestClient

from app.ai.ai_restaurant.role_resolver import resolve_user_role
from app.config import admin_api_settings
from app.firestore_db.firebase_schemas import Configurations
from app.services import worker_entry
from app.services.phones import normalize_phone
from ops.migrate_phones import planned_changes
from tests.conftest import seed_business, seed_configuration

client = TestClient(worker_entry.app)
HEADERS = {"X-API-Key": admin_api_settings.API_KEY}


def test_normalize_phone():
    assert normalize_phone("+57 300 123-4567") == "573001234567"
    assert normalize_phone("3001234567") == "573001234567"
    assert normalize_phone("573001234567") == "573001234567"
    assert normalize_phone("+1 (555) 010-1234") == "15550101234"
    assert normalize_phone(None) == ""


def test_an_admin_saved_with_another_format_is_still_an_admin(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, admin_phone_numbers=["+57 300 123 4567"])

    assert resolve_user_role(business.bussiness_id, "573001234567")["role"] == "admin"
    assert resolve_user_role(business.bussiness_id, "573009999999")["role"] == "cliente"


def test_the_api_stores_admin_phones_as_digits_without_duplicates(fake_store):
    business = seed_business(fake_store)
    configuration = seed_configuration(fake_store, business)
    base = f"/api/v1/admin/configurations/{business.bussiness_id}"

    response = client.put(f"{base}/admin-phone-numbers", json={"phone_numbers": ["300 123 4567", "+573001234567"]}, headers=HEADERS)
    patched = client.patch(base, json={"manager_phone": "+57 (310) 555-0000"}, headers=HEADERS)
    bad = client.put(f"{base}/admin-phone-numbers", json={"phone_numbers": ["abc"]}, headers=HEADERS)

    assert response.status_code == patched.status_code == 200
    stored = fake_store.stores[Configurations][configuration.configuration_id]
    assert stored.admin_phone_numbers == ["573001234567"]
    assert stored.manager_phone == "573105550000"
    assert bad.status_code == 400


def test_the_migration_only_plans_documents_that_change():
    docs = [
        ("ok", {"manager_phone": "573001234567", "admin_phone_numbers": ["573001234567"]}),
        ("viejo", {"manager_phone": "300 123 4567", "admin_phone_numbers": ["+57 300 123 4567", "3001234567"]}),
    ]

    assert planned_changes(docs) == [
        ("viejo", {"manager_phone": "573001234567", "admin_phone_numbers": ["573001234567"]})
    ]


def test_a_username_customer_has_no_contact_phone_until_they_give_one():
    from types import SimpleNamespace
    from app.services.phones import contact_phone, is_username_id

    assert is_username_id("CO.1780159176423859") and not is_username_id("573001234567")
    assert not is_username_id(None)
    assert contact_phone(SimpleNamespace(phone_number="CO.1780159176423859", contact_phone=None)) is None
    assert contact_phone(SimpleNamespace(phone_number="CO.1780159176423859", contact_phone="573001")) == "573001"
    assert contact_phone(SimpleNamespace(phone_number="573009")) == "573009"
