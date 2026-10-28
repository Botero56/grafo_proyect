"""Heartbeat de las Raspberries: el webhook público solo acepta el token del
negocio y encola; el worker guarda el estado actual; el panel ve un estado
calculado ahora, nunca uno viejo como si fuera actual."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import admin_api_settings
from app.firestore_db.firebase_schemas import DeviceEvents, Devices
from app.schemas.api.device_schemas import HeartbeatPayload
from app.schemas.enum_schemas import DeviceStatus
from app.services import secrets, webhook_entry, worker_entry
from app.services.devices import devices
from tests.conftest import seed_business, seed_configuration

TOKEN = "token-de-la-raspberry-0001"
NOW = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
webhook = TestClient(webhook_entry.app)
worker = TestClient(worker_entry.app)


def _heartbeat(**overrides) -> dict:
    body = {
        "device_id": "rpi-abc123",
        "measured_at": "2026-10-25T15:00:00+00:00",
        "version": "1.2.0",
        "commit": "e1f2696",
        "printer_usb": True,
        "wifi_ssid": "Restaurante",
        "uptime_seconds": 3600,
        "last_print_ok_at": None,
        "last_error": None,
        "last_error_at": None,
    }
    return {**body, **overrides}


def _token_for(business_id: str, token: str = TOKEN) -> None:
    secrets._cache[(secrets.secret_ref(secrets.PRINTER_TOKEN, business_id), False)] = ([token], float("inf"))


def _post(business_id: str, token: str = TOKEN, body: dict | None = None):
    headers = {"Authorization": f"Bearer {token}", "X-Business-Id": business_id}
    return webhook.post("/devices/heartbeat", json=body or _heartbeat(), headers=headers)


# --- webhook --------------------------------------------------------------


def test_heartbeat_valido_se_encola_sin_el_token(fake_store):
    _token_for("negocio1")
    with patch("app.services.tasks.cloud_tasks_client.enqueue_device_heartbeat") as enqueue:
        response = _post("negocio1")
    assert response.status_code == 202
    business_id, heartbeat = enqueue.call_args.args
    assert business_id == "negocio1" and heartbeat["device_id"] == "rpi-abc123"
    assert TOKEN not in str(heartbeat)


def test_token_de_otro_negocio_da_401(fake_store):
    _token_for("negocio1")
    _token_for("negocio2", "token-del-otro-negocio-0002")
    with patch("app.services.tasks.cloud_tasks_client.enqueue_device_heartbeat") as enqueue:
        assert _post("negocio2").status_code == 401
        assert _post("negocio1", token="token-equivocado-000000").status_code == 401
        assert webhook.post("/devices/heartbeat", json=_heartbeat()).status_code == 401
    enqueue.assert_not_called()


def test_negocio_sin_token_o_id_raro_da_401(fake_store):
    with patch("app.services.tasks.cloud_tasks_client.enqueue_device_heartbeat") as enqueue, patch.object(
        secrets, "get_secret_or_none", return_value=None
    ):
        assert _post("no-existe").status_code == 401
        assert _post("../../x").status_code == 401
    enqueue.assert_not_called()


def test_cuerpo_invalido_da_422(fake_store):
    _token_for("negocio1")
    with patch("app.services.tasks.cloud_tasks_client.enqueue_device_heartbeat") as enqueue:
        assert _post("negocio1", body=_heartbeat(device_id="con espacios/")).status_code == 422
        assert _post("negocio1", body=_heartbeat(printer_usb="quizá")).status_code == 422
    enqueue.assert_not_called()


# --- worker ---------------------------------------------------------------


def test_worker_guarda_el_estado_actual(fake_store):
    body = {"business_id": "negocio1", "heartbeat": _heartbeat()}
    assert worker.post("/tasks/device-heartbeat", json=body).status_code == 200
    stored = fake_store.stores[Devices]["negocio1--rpi-abc123"]
    assert stored.version == "1.2.0" and stored.wifi_ssid == "Restaurante"
    # El segundo heartbeat reemplaza al primero: es estado, no historial.
    body["heartbeat"] = _heartbeat(measured_at="2026-10-25T15:01:00+00:00", printer_usb=False)
    worker.post("/tasks/device-heartbeat", json=body)
    assert len(fake_store.stores[Devices]) == 1
    assert fake_store.stores[Devices]["negocio1--rpi-abc123"].printer_usb is False


def test_mismo_device_id_en_dos_negocios_no_se_pisa(fake_store):
    for business_id in ("negocio1", "negocio2"):
        devices.record_heartbeat(business_id, HeartbeatPayload(**_heartbeat()), NOW)
    assert set(fake_store.stores[Devices]) == {"negocio1--rpi-abc123", "negocio2--rpi-abc123"}


# --- estado ---------------------------------------------------------------


def _device(**fields) -> Devices:
    device = devices.record_heartbeat("negocio1", HeartbeatPayload(**_heartbeat(**fields)), NOW)
    return device


def test_estados(fake_store):
    assert devices.device_status(_device(), NOW + timedelta(seconds=60)) == DeviceStatus.OPERATIVO
    # Un heartbeat perdido no es una caída; varios sí.
    assert devices.device_status(_device(), NOW + timedelta(seconds=150)) == DeviceStatus.OPERATIVO
    assert devices.device_status(_device(), NOW + timedelta(minutes=10)) == DeviceStatus.DESCONECTADO
    assert devices.device_status(_device(printer_usb=False), NOW) == DeviceStatus.DEGRADADO
    failed = _device(last_print_ok_at="2026-10-25T14:00:00Z", last_error="impresora no conectada",
                     last_error_at="2026-10-25T14:30:00Z")
    assert devices.device_status(failed, NOW) == DeviceStatus.DEGRADADO
    recovered = _device(last_print_ok_at="2026-10-25T14:40:00Z", last_error_at="2026-10-25T14:30:00Z")
    assert devices.device_status(recovered, NOW) == DeviceStatus.OPERATIVO
    assert devices.device_status(Devices(), NOW) == DeviceStatus.SIN_INFORMACION


# --- panel ----------------------------------------------------------------


def test_panel_lista_solo_los_dispositivos_del_negocio(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    devices.record_heartbeat(business.bussiness_id, HeartbeatPayload(**_heartbeat()))
    devices.record_heartbeat("otro-negocio", HeartbeatPayload(**_heartbeat(device_id="rpi-ajeno")))

    response = worker.get(
        f"/api/v1/admin/configurations/{business.bussiness_id}/printer/devices",
        headers={"X-API-Key": admin_api_settings.API_KEY},
    )
    assert response.status_code == 200
    [device] = response.json()
    assert device["device_id"] == "rpi-abc123" and device["status"] == "operativo"
    assert device["seconds_since_seen"] is not None


def test_panel_pide_api_key(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    url = f"/api/v1/admin/configurations/{business.bussiness_id}/printer/devices"
    assert worker.get(url, headers={"X-API-Key": "otra"}).status_code == 401


# --- historial ------------------------------------------------------------


def _beat(at: datetime, **fields) -> None:
    devices.record_heartbeat("negocio1", HeartbeatPayload(**_heartbeat(**fields)), at)


def _events(fake_store) -> list[tuple[str, str]]:
    events = sorted(fake_store.stores[DeviceEvents].values(), key=lambda e: e.at)
    return [(e.event_type, e.detail) for e in events]


def test_heartbeats_iguales_no_llenan_el_historial(fake_store):
    for minute in range(5):
        _beat(NOW + timedelta(minutes=minute), uptime_seconds=3600 + minute * 60)
    assert _events(fake_store) == [("registrada", "Primera conexión (versión 1.2.0)")]


def test_corte_de_internet_vs_corte_de_luz(fake_store):
    _beat(NOW, uptime_seconds=3600)
    # 20 min sin heartbeat y el uptime siguió contando: fue el internet.
    _beat(NOW + timedelta(minutes=20), uptime_seconds=3600 + 1200)
    # 30 min después vuelve con uptime de 5 min: se reinició, fue la luz.
    _beat(NOW + timedelta(minutes=50), uptime_seconds=300)
    cortes = [e for e in fake_store.stores[DeviceEvents].values() if e.event_type == "sin_conexion"]
    cortes.sort(key=lambda e: e.at)
    assert [(c.at, c.until) for c in cortes] == [
        (NOW, NOW + timedelta(minutes=20)),
        (NOW + timedelta(minutes=20), NOW + timedelta(minutes=50)),
    ]
    assert "internet" in cortes[0].detail and "20 min" in cortes[0].detail
    assert "luz" in cortes[1].detail


def test_reinicio_rapido_sin_caida(fake_store):
    _beat(NOW, uptime_seconds=3600)
    _beat(NOW + timedelta(seconds=90), uptime_seconds=40)
    assert ("reiniciada", "La Raspberry se reinició") in _events(fake_store)


def test_impresora_wifi_version_y_error(fake_store):
    _beat(NOW)
    _beat(NOW + timedelta(minutes=1), printer_usb=False, wifi_ssid="Hotspot", version="1.3.0",
          last_error="impresora no conectada", last_error_at="2026-10-25T15:00:30Z")
    _beat(NOW + timedelta(minutes=2), printer_usb=True, wifi_ssid="Hotspot", version="1.3.0",
          last_error="impresora no conectada", last_error_at="2026-10-25T15:00:30Z")
    types = [t for t, _ in _events(fake_store)]
    assert types.count("error_impresion") == 1  # el mismo error no se repite
    assert {"impresora_desconectada", "impresora_conectada", "wifi_cambiado", "version_actualizada"} <= set(types)
    assert ("wifi_cambiado", "Wi-Fi: Restaurante → Hotspot") in _events(fake_store)


def test_panel_ve_el_historial_de_su_negocio_del_mas_nuevo_al_mas_viejo(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    biz = business.bussiness_id
    devices.record_heartbeat(biz, HeartbeatPayload(**_heartbeat()), NOW)
    devices.record_heartbeat(biz, HeartbeatPayload(**_heartbeat(printer_usb=False)), NOW + timedelta(minutes=1))
    devices.record_heartbeat("otro-negocio", HeartbeatPayload(**_heartbeat()), NOW)

    response = worker.get(f"/api/v1/admin/configurations/{biz}/printer/events",
                          headers={"X-API-Key": admin_api_settings.API_KEY})
    assert response.status_code == 200
    assert [e["event_type"] for e in response.json()] == ["impresora_desconectada", "registrada"]
    assert worker.get(f"/api/v1/admin/configurations/{biz}/printer/events",
                      headers={"X-API-Key": "otra"}).status_code == 401
