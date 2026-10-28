"""Cada comanda va a la impresora de SU negocio, autenticada con el token de
ese negocio; sin impresora (o con la impresora caída) va al chat del admin."""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.config import admin_api_settings, printer_settings
from app.firestore_db.firebase_schemas import Configurations, Orders, PrintJobs
from app.schemas.enum_schemas import PrintJobStatus
from app.services import secrets, worker_entry
from app.services.printer_client import print_jobs
from app.services.printer_client.print_jobs import build_ticket, send_print_job
from tests.conftest import seed_business, seed_configuration, seed_printer

client = TestClient(worker_entry.app)
HEADERS = {"X-API-Key": admin_api_settings.API_KEY}
SHARED_URL = "https://raspberry.test/print"


def _job(fake_store, business):
    order = Orders()
    order.business_id = business.bussiness_id
    fake_store.seed(order)
    job = PrintJobs()
    job.business_id = business.bussiness_id
    job.order_id = order.order_id
    job.content = build_ticket(business, None, order, "1x Menú del día")
    job.attempts = 0
    return fake_store.seed(job)


async def _send(job, post_status=200):
    response = MagicMock(status_code=post_status)
    response.raise_for_status = MagicMock(side_effect=None if post_status < 400 else RuntimeError("caída"))
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)) as post, patch.object(
        print_jobs.HandoffService, "notify_admins", new=AsyncMock(return_value=True)
    ) as notify:
        result = await send_print_job(job, "efectivo")
    return result, post, notify


async def test_an_order_of_a_never_reaches_the_printer_of_b(fake_store):
    a = seed_business(fake_store, name="Restaurante A")
    b = seed_business(fake_store, name="Restaurante B")
    seed_printer(fake_store, a, url="https://a.test/print", token="token-de-a-000000001")
    seed_printer(fake_store, b, url="https://b.test/print", token="token-de-b-000000001")

    printed, post, _ = await _send(_job(fake_store, a))

    assert printed is True
    post.assert_awaited_once()
    assert post.await_args.args[0] == "https://a.test/print"
    headers = post.await_args.kwargs["headers"]
    assert headers == {"Authorization": "Bearer token-de-a-000000001", "X-Business-Id": a.bussiness_id}


async def test_a_shared_endpoint_prints_each_ticket_with_its_own_header(fake_store):
    a = seed_business(fake_store, name="Restaurante A")
    b = seed_business(fake_store, name="Restaurante B")
    seed_printer(fake_store, a, url=SHARED_URL, token="token-de-a-000000001")
    seed_printer(fake_store, b, url=SHARED_URL, token="token-de-b-000000001")

    _, post_a, _ = await _send(_job(fake_store, a))
    _, post_b, _ = await _send(_job(fake_store, b))

    text_a = post_a.await_args.kwargs["json"]["text"]
    text_b = post_b.await_args.kwargs["json"]["text"]
    assert "RESTAURANTE A" in text_a and "RESTAURANTE B" not in text_a
    assert "RESTAURANTE B" in text_b and "RESTAURANTE A" not in text_b
    assert post_a.await_args.kwargs["headers"]["X-Business-Id"] == a.bussiness_id
    assert post_b.await_args.kwargs["headers"]["Authorization"] == "Bearer token-de-b-000000001"


async def test_business_without_printer_sends_the_ticket_to_the_admin(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)

    sent, post, notify = await _send(_job(fake_store, business))

    assert sent is True
    post.assert_not_awaited()
    assert notify.await_args.args[1].startswith("🧾 Nuevo pedido confirmado")


async def test_printer_down_sends_the_ticket_to_the_admin(fake_store):
    business = seed_business(fake_store)
    seed_printer(fake_store, business)
    job = _job(fake_store, business)

    printed, post, notify = await _send(job, post_status=503)

    assert printed is False
    post.assert_awaited_once()
    assert notify.await_args.args[1].startswith("⚠️ No se pudo imprimir una comanda.")
    assert job.status == PrintJobStatus.FAILED.value


async def test_printer_without_readable_token_never_prints_unauthenticated(fake_store):
    business = seed_business(fake_store)
    configuration = seed_printer(fake_store, business)
    configuration.printer_settings = {**configuration.printer_settings, "auth_secret_ref": None}

    printed, post, notify = await _send(_job(fake_store, business))

    assert printed is False
    post.assert_not_awaited()
    notify.assert_awaited_once()


async def test_legacy_url_only_serves_the_allowed_business(fake_store, monkeypatch):
    legacy = seed_business(fake_store, name="Legacy")
    other = seed_business(fake_store, name="Otro")
    monkeypatch.setattr(printer_settings, "LEGACY_PRINT_URL", "https://legacy.test/print")
    monkeypatch.setattr(printer_settings, "LEGACY_PRINT_BUSINESS_ID", legacy.bussiness_id)

    _, post, _ = await _send(_job(fake_store, legacy))
    assert post.await_args.args[0] == "https://legacy.test/print"

    _, post, notify = await _send(_job(fake_store, other))
    post.assert_not_awaited()
    notify.assert_awaited_once()


# --- API admin ---------------------------------------------------------------


def _url(business_id):
    return f"/api/v1/admin/configurations/{business_id}/printer"


def _fake_put(kind, business_id, value):
    return f"projects/p/secrets/{kind}--{business_id}"


def test_put_stores_the_token_in_secret_manager_and_never_returns_it(fake_store):
    business = seed_business(fake_store)
    configuration = seed_configuration(fake_store, business)
    token = "token-impresora-muy-secreto-1"

    with patch.object(secrets, "put_secret", side_effect=_fake_put) as put:
        response = client.put(
            _url(business.bussiness_id),
            json={"enabled": True, "endpoint_url": "https://p.test/print", "token": token, "copies": 2},
            headers=HEADERS,
        )

    assert response.status_code == 200, response.text
    assert token not in response.text
    assert response.json()["token_configured"] is True and response.json()["copies"] == 2
    put.assert_called_once_with(secrets.PRINTER_TOKEN, business.bussiness_id, token)
    stored = fake_store.stores[Configurations][configuration.configuration_id]
    assert token not in str(stored.printer_settings)
    assert token not in client.get(_url(business.bussiness_id), headers=HEADERS).text


def test_put_without_token_keeps_the_current_one(fake_store):
    business = seed_business(fake_store)
    seed_printer(fake_store, business)

    with patch.object(secrets, "put_secret") as put:
        response = client.put(
            _url(business.bussiness_id), json={"enabled": True, "endpoint_url": "https://new.test/print"}, headers=HEADERS
        )

    assert response.status_code == 200
    put.assert_not_called()
    assert response.json()["token_configured"] is True


def test_put_rejects_insecure_urls_short_tokens_and_enabling_without_token(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    url = _url(business.bussiness_id)

    with patch.object(secrets, "put_secret") as put:
        http = client.put(url, json={"enabled": True, "endpoint_url": "http://p.test/print", "token": "x" * 20}, headers=HEADERS)
        short = client.put(url, json={"enabled": True, "endpoint_url": "https://p.test/print", "token": "corta123"}, headers=HEADERS)
        no_token = client.put(url, json={"enabled": True, "endpoint_url": "https://p.test/print"}, headers=HEADERS)

    assert http.status_code == short.status_code == no_token.status_code == 400
    assert "corta123" not in short.text
    put.assert_not_called()


def test_patch_configuration_can_no_longer_write_printer_settings(fake_store):
    business = seed_business(fake_store)
    configuration = seed_configuration(fake_store, business)

    response = client.patch(
        f"/api/v1/admin/configurations/{business.bussiness_id}",
        json={"printer_settings": {"enabled": True, "endpoint_url": "https://evil.test", "auth_secret_ref": "otro"}},
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert not fake_store.stores[Configurations][configuration.configuration_id].printer_settings
