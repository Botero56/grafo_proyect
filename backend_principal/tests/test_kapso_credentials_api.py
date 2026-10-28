"""Alta de las credenciales de Kapso de un negocio: los valores van a Secret
Manager, Firestore guarda solo la referencia y ninguna respuesta los
devuelve."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import admin_api_settings
from app.firestore_db.firebase_schemas import Businesses
from app.services import secrets, worker_entry
from tests.conftest import seed_business

client = TestClient(worker_entry.app)
HEADERS = {"X-API-Key": admin_api_settings.API_KEY}
API_KEY = "kapso-api-key-de-prueba-0001"
WEBHOOK_SECRET = "kapso-webhook-secret-prueba-01"


def _url(business_id: str) -> str:
    return f"/api/v1/admin/businesses/{business_id}/kapso-credentials"


def _fake_put(kind, business_id, value):
    return f"projects/p/secrets/{kind}--{business_id}"


def test_stores_only_references_and_never_returns_the_values(fake_store):
    business = seed_business(fake_store)

    with patch.object(secrets, "put_secret", side_effect=_fake_put) as put:
        response = client.put(
            _url(business.bussiness_id), json={"api_key": API_KEY, "webhook_secret": WEBHOOK_SECRET}, headers=HEADERS
        )

    assert response.status_code == 200
    assert response.json() == {"api_key_configured": True, "webhook_secret_configured": True}
    assert API_KEY not in response.text and WEBHOOK_SECRET not in response.text
    put.assert_any_call(secrets.KAPSO_API_KEY, business.bussiness_id, API_KEY)
    stored = fake_store.stores[Businesses][business.bussiness_id]
    assert stored.kapso_api_key_secret == f"projects/p/secrets/kapso-api-key--{business.bussiness_id}"
    assert stored.kapso_webhook_secret_secret.endswith(f"kapso-webhook-secret--{business.bussiness_id}")

    status = client.get(_url(business.bussiness_id), headers=HEADERS)
    assert status.json() == {"api_key_configured": True, "webhook_secret_configured": True}


def test_rotating_one_value_keeps_the_other(fake_store):
    business = seed_business(fake_store)
    with patch.object(secrets, "put_secret", side_effect=_fake_put) as put:
        client.put(_url(business.bussiness_id), json={"webhook_secret": WEBHOOK_SECRET}, headers=HEADERS)

    put.assert_called_once()
    assert client.get(_url(business.bussiness_id), headers=HEADERS).json() == {
        "api_key_configured": False,
        "webhook_secret_configured": True,
    }


def test_short_or_empty_values_are_rejected_without_echoing_them(fake_store):
    business = seed_business(fake_store)
    with patch.object(secrets, "put_secret") as put:
        short = client.put(_url(business.bussiness_id), json={"api_key": "corta123"}, headers=HEADERS)
        empty = client.put(_url(business.bussiness_id), json={}, headers=HEADERS)

    assert short.status_code == 400 and "corta123" not in short.text
    assert empty.status_code == 400
    put.assert_not_called()


def test_requires_the_admin_api_key(fake_store):
    business = seed_business(fake_store)
    response = client.put(_url(business.bussiness_id), json={"api_key": API_KEY}, headers={"X-API-Key": "incorrecta"})
    assert response.status_code == 401


def test_unknown_business_is_404(fake_store):
    with patch.object(secrets, "put_secret") as put:
        response = client.put(_url("no-existe"), json={"api_key": API_KEY}, headers=HEADERS)
    assert response.status_code == 404
    put.assert_not_called()
