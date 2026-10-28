"""Versión y actualización de la Raspberry desde el panel: van a la URL de SU
negocio, con su token, y los errores de la Raspberry no se confunden con una
sesión vencida del panel."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import admin_api_settings
from app.services import worker_entry
from tests.conftest import seed_business, seed_configuration, seed_printer

client = TestClient(worker_entry.app)
HEADERS = {"X-API-Key": admin_api_settings.API_KEY}
TOKEN = "token-de-la-raspberry-01"


def _url(business_id, action):
    return f"/api/v1/admin/configurations/{business_id}/printer/{action}"


def _respond(status_code=200, **kwargs):
    return AsyncMock(return_value=httpx.Response(status_code, request=httpx.Request("GET", "https://x"), **kwargs))


def test_version_goes_to_the_business_raspberry_with_its_token(fake_store):
    business = seed_business(fake_store)
    seed_printer(fake_store, business, url="https://rpi.test/print", token=TOKEN)

    with patch("httpx.AsyncClient.request", new=_respond(json={"version": "1.1.0"})) as request:
        response = client.get(_url(business.bussiness_id, "device"), headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {"version": "1.1.0"}
    method, target = request.await_args.args
    assert (method, target) == ("GET", "https://rpi.test/version")
    sent = request.await_args.kwargs["headers"]
    assert sent == {"Authorization": f"Bearer {TOKEN}", "X-Business-Id": business.bussiness_id}
    assert TOKEN not in response.text


def test_update_posts_to_update(fake_store):
    business = seed_business(fake_store)
    seed_printer(fake_store, business, url="https://rpi.test/print", token=TOKEN)
    result = {"updated": True, "from": "1.0.0", "to": "1.1.0", "commit": "abc"}

    with patch("httpx.AsyncClient.request", new=_respond(json=result)) as request:
        response = client.post(_url(business.bussiness_id, "update"), headers=HEADERS)

    assert response.json() == result
    assert request.await_args.args == ("POST", "https://rpi.test/update")


def test_without_printer_configured_is_409(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)

    with patch("httpx.AsyncClient.request") as request:
        response = client.post(_url(business.bussiness_id, "update"), headers=HEADERS)

    assert response.status_code == 409
    request.assert_not_called()


def test_raspberry_401_becomes_502_with_its_detail(fake_store):
    business = seed_business(fake_store)
    seed_printer(fake_store, business, token=TOKEN)

    with patch("httpx.AsyncClient.request", new=_respond(401, json={"detail": "token inválido"})):
        response = client.post(_url(business.bussiness_id, "update"), headers=HEADERS)

    assert response.status_code == 502
    assert "401" in response.json()["detail"] and "token inválido" in response.json()["detail"]


def test_raspberry_down_or_slow(fake_store):
    business = seed_business(fake_store)
    seed_printer(fake_store, business, token=TOKEN)

    with patch("httpx.AsyncClient.request", new=AsyncMock(side_effect=httpx.ConnectError("caída"))):
        assert client.get(_url(business.bussiness_id, "device"), headers=HEADERS).status_code == 502
    with patch("httpx.AsyncClient.request", new=AsyncMock(side_effect=httpx.ReadTimeout("lenta"))):
        assert client.post(_url(business.bussiness_id, "update"), headers=HEADERS).status_code == 504


def test_requires_the_admin_api_key(fake_store):
    business = seed_business(fake_store)
    seed_printer(fake_store, business, token=TOKEN)

    with patch("httpx.AsyncClient.request") as request:
        assert client.post(_url(business.bussiness_id, "update"), headers={"X-API-Key": "otra"}).status_code == 401
    request.assert_not_called()


WIFI = {"ssid": "Edy Gourmet", "password": "clave-del-local"}


def test_wifi_is_forwarded_to_the_raspberry_without_the_actor(fake_store):
    business = seed_business(fake_store)
    seed_printer(fake_store, business, url="https://rpi.test/print", token=TOKEN)

    with patch("httpx.AsyncClient.request", new=_respond(json={"saved": True, "ssid": "Edy Gourmet"})) as request:
        response = client.post(_url(business.bussiness_id, "wifi"), headers=HEADERS, json={**WIFI, "actor": "a@b.co"})

    assert response.json() == {"saved": True, "ssid": "Edy Gourmet"}
    assert request.await_args.args == ("POST", "https://rpi.test/wifi")
    assert request.await_args.kwargs["json"] == WIFI


@pytest.mark.parametrize("body", [{"ssid": "", "password": "clave-del-local"}, {"ssid": "Edy", "password": "corta"}])
def test_invalid_wifi_never_echoes_the_password(fake_store, body):
    business = seed_business(fake_store)
    seed_printer(fake_store, business, token=TOKEN)

    with patch("httpx.AsyncClient.request") as request:
        response = client.post(_url(business.bussiness_id, "wifi"), headers=HEADERS, json=body)

    assert response.status_code == 422
    assert body["password"] not in response.text
    request.assert_not_called()


def test_test_print_sends_a_short_ticket_to_print(fake_store):
    business = seed_business(fake_store)
    seed_printer(fake_store, business, url="https://rpi.test/print", token=TOKEN)

    with patch("httpx.AsyncClient.request", new=_respond(json={"ok": True})) as request:
        response = client.post(_url(business.bussiness_id, "test"), headers=HEADERS)

    assert response.json() == {"ok": True}
    assert request.await_args.args == ("POST", "https://rpi.test/print")
    sent = request.await_args.kwargs["json"]
    assert sent["text"].startswith("*** PRUEBA DE IMPRESION ***") and sent["copies"] == 1


def test_test_print_without_usb_printer_is_502_with_detail(fake_store):
    business = seed_business(fake_store)
    seed_printer(fake_store, business, token=TOKEN)

    with patch("httpx.AsyncClient.request", new=_respond(503, json={"detail": "impresora no conectada"})):
        response = client.post(_url(business.bussiness_id, "test"), headers=HEADERS)

    assert response.status_code == 502 and "impresora no conectada" in response.json()["detail"]
