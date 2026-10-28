"""URL del webhook por tenant: se genera al crear el negocio, se rota y la
clave nunca se repite."""

import re

from fastapi.testclient import TestClient

from app.config import admin_api_settings, whatsapp_settings
from app.firestore_db.firebase_schemas import Businesses
from app.services import tenants, worker_entry
from tests.conftest import seed_business

client = TestClient(worker_entry.app)
HEADERS = {"X-API-Key": admin_api_settings.API_KEY}


def test_new_tenant_key_has_a_readable_slug_and_192_random_bits():
    key = tenants.new_tenant_key("Edy Gourmet & Cía. Ñ")
    slug, random_part = key.split("_", 1)
    assert slug == "edygourmetca"
    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", random_part)  # 24 bytes en base64url
    assert tenants.new_tenant_key("Edy") != tenants.new_tenant_key("Edy")
    assert tenants.new_tenant_key("!!!").startswith("tenant_")


def test_create_business_generates_the_key(fake_store):
    response = client.post(
        "/api/v1/admin/businesses",
        json={
            "name": "Edy Gourmet",
            "legal_name": "Edy SAS",
            "bussiness_vertical": "restaurant",
            "phone": "573001112233",
            "whatsapp_phone_number": "573001112233",
            "whatsapp_phone_number_id": "1344009488801289",
        },
        headers=HEADERS,
    )
    assert response.status_code == 201
    stored = fake_store.stores[Businesses][response.json()["bussiness_id"]]
    assert stored.webhook_tenant_key.startswith("edygourmet_")
    assert stored.webhook_tenant_key_rotated_at is not None


def test_rotate_replaces_the_key_and_get_shows_the_url(fake_store, monkeypatch):
    monkeypatch.setattr(whatsapp_settings, "WEBHOOK_PUBLIC_BASE_URL", "https://hook.example/")
    business = seed_business(fake_store)
    base = f"/api/v1/admin/businesses/{business.bussiness_id}/webhook-url"

    assert client.get(base, headers=HEADERS).json() == {"url": None, "rotated_at": None}

    first = client.post(f"{base}/rotate", headers=HEADERS).json()["url"]
    second = client.post(f"{base}/rotate", headers=HEADERS).json()["url"]

    assert first.startswith("https://hook.example/webhook/kapso/edygourmet_")
    assert first != second
    current = client.get(base, headers=HEADERS).json()
    assert current["url"] == second and current["rotated_at"]


def test_webhook_url_requires_the_admin_api_key(fake_store):
    business = seed_business(fake_store)
    response = client.post(
        f"/api/v1/admin/businesses/{business.bussiness_id}/webhook-url/rotate", headers={"X-API-Key": "x"}
    )
    assert response.status_code == 401
