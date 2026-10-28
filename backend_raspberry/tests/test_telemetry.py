import json

import pytest
from fastapi.testclient import TestClient

from app import hardware, main, telemetry

TOKEN = "token-impresora-0001"
BUSINESS = "negocio-1"
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Business-Id": BUSINESS}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PRINTER_TOKEN", TOKEN)
    monkeypatch.setenv("BUSINESS_ID", BUSINESS)
    monkeypatch.setenv("DEVICE_ID", "rpi-prueba")
    monkeypatch.setattr(hardware, "check_printer", lambda: True)
    monkeypatch.setattr(telemetry, "wifi_ssid", lambda: "Restaurante")
    monkeypatch.setattr(telemetry, "current_commit", lambda: "abc1234")


def test_health_pide_token(env):
    client = TestClient(main.app)
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={**AUTH, "X-Business-Id": "otro"}).status_code == 403


def test_health_devuelve_el_estado(env):
    body = TestClient(main.app).get("/health", headers=AUTH).json()
    assert body["device_id"] == "rpi-prueba"
    assert body["printer_usb"] is True
    assert body["wifi_ssid"] == "Restaurante"
    assert body["version"] == main.current_version()
    assert TOKEN not in json.dumps(body)


def test_print_registra_exito_y_error(env, monkeypatch):
    client = TestClient(main.app)
    monkeypatch.setattr(hardware, "print_text", lambda *a: None)
    client.post("/print", json={"text": "x"}, headers=AUTH)
    assert telemetry.snapshot("1")["last_print_ok_at"]

    monkeypatch.setattr(hardware, "check_printer", lambda: False)
    client.post("/print", json={"text": "x"}, headers=AUTH)
    snap = telemetry.snapshot("1")
    assert snap["last_error"] == "impresora no conectada" and snap["last_error_at"]


def test_heartbeat_manda_token_en_cabecera_y_no_en_el_cuerpo(env, monkeypatch):
    enviado = {}

    class Respuesta:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(request, timeout):
        enviado.update(url=request.full_url, headers=dict(request.header_items()), body=request.data.decode())
        return Respuesta()

    monkeypatch.setenv("HEARTBEAT_URL", "https://backend.test/devices/heartbeat")
    monkeypatch.setattr(telemetry.urllib.request, "urlopen", fake_urlopen)
    assert telemetry.send_heartbeat("1.2.0") is True
    assert enviado["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert enviado["headers"]["X-business-id"] == BUSINESS
    assert TOKEN not in enviado["body"]
    assert json.loads(enviado["body"])["device_id"] == "rpi-prueba"


def test_heartbeat_sin_https_no_sale(env, monkeypatch):
    monkeypatch.setenv("HEARTBEAT_URL", "http://backend.test/x")
    monkeypatch.setattr(telemetry.urllib.request, "urlopen", lambda *a, **k: pytest.fail("no debía salir"))
    assert telemetry.send_heartbeat("1") is False


def test_heartbeat_caido_no_rompe(env, monkeypatch):
    monkeypatch.setenv("HEARTBEAT_URL", "https://backend.test/x")
    def boom(*a, **k): raise OSError("sin red")
    monkeypatch.setattr(telemetry.urllib.request, "urlopen", boom)
    assert telemetry.send_heartbeat("1") is False


def test_device_id_estable_sin_env(monkeypatch, tmp_path):
    monkeypatch.delenv("DEVICE_ID", raising=False)
    first = telemetry.device_id()
    assert first.startswith("rpi-") and first == telemetry.device_id()


def test_url_por_defecto_y_apagado(monkeypatch):
    monkeypatch.delenv("HEARTBEAT_URL", raising=False)
    assert telemetry.heartbeat_url() == telemetry.DEFAULT_HEARTBEAT_URL
    monkeypatch.setenv("HEARTBEAT_URL", "off")
    assert telemetry.heartbeat_url() == "" and telemetry.start_heartbeat_loop("1") is None
