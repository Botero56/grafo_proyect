"""El worker solo acepta tareas firmadas por NUESTRA cuenta de servicio y
para NUESTRO worker, y entiende el sobre {business_id, event}."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import cloud_tasks_settings
from app.services import worker_entry
from app.services.firestore_services.firestore_webhook_service import find_business
from app.services.tasks import task_auth
from tests.conftest import PHONE_NUMBER_ID, seed_business

client = TestClient(worker_entry.app)
SA = "tasks@p.iam.gserviceaccount.com"
BODY = {"business_id": "b1", "event": {"entry": []}}


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr(cloud_tasks_settings, "CLOUD_TASKS_EMULATOR_HOST", "")
    monkeypatch.setattr(cloud_tasks_settings, "WORKER_SERVICE_ACCOUNT_EMAIL", SA)
    monkeypatch.setattr(cloud_tasks_settings, "WORKER_URL", "https://worker.run.app")
    monkeypatch.setattr(task_auth, "_get_transport", lambda: None)
    with patch.object(
        worker_entry.inbound_pipeline, "process_incoming_message", new=AsyncMock(return_value=None)
    ) as process:
        yield process


def _post(claims=None, error=None):
    with patch("google.oauth2.id_token.verify_token", return_value=claims, side_effect=error) as verify:
        response = client.post("/tasks/process-event", json=BODY, headers={"Authorization": "Bearer tkn"})
    return response, verify


def test_token_from_our_service_account_passes(production):
    response, verify = _post({"email": SA, "email_verified": True})
    assert response.status_code == 200
    assert verify.call_args.kwargs["audience"] == [
        "https://worker.run.app",
        "https://worker.run.app/tasks/process-event",
    ]
    production.assert_awaited_once()


def test_token_from_another_account_is_401(production):
    response, _ = _post({"email": "otra@p.iam.gserviceaccount.com", "email_verified": True})
    assert response.status_code == 401
    production.assert_not_awaited()


def test_while_changing_accounts_both_are_accepted_and_tasks_sign_with_the_first(production, monkeypatch):
    from app.services.tasks import cloud_tasks_client

    old = "viejo@p.iam.gserviceaccount.com"
    monkeypatch.setattr(cloud_tasks_settings, "WORKER_SERVICE_ACCOUNT_EMAIL", f"{SA}, {old}")

    assert _post({"email": old, "email_verified": True})[0].status_code == 200
    assert _post({"email": SA, "email_verified": True})[0].status_code == 200
    assert _post({"email": "otra@p.iam.gserviceaccount.com", "email_verified": True})[0].status_code == 401
    assert cloud_tasks_client._oidc()["oidc_token"]["service_account_email"] == SA


def test_invalid_or_missing_token_is_401(production):
    assert _post(error=ValueError("firma inválida"))[0].status_code == 401
    assert client.post("/tasks/process-event", json=BODY).status_code == 401
    production.assert_not_awaited()


def test_find_business_trusts_the_envelope_id_only_for_its_own_number(fake_store):
    business = seed_business(fake_store)
    assert find_business("otro-display", PHONE_NUMBER_ID, business.bussiness_id) is business
    assert find_business(None, None, business.bussiness_id) is business
    # Un id válido no habilita a escribir en las conversaciones de otro número.
    assert find_business(None, "999", business.bussiness_id) is None
    assert find_business(None, None, "no-existe") is None
