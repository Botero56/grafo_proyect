import subprocess

import pytest
from fastapi.testclient import TestClient

from app import hardware, main

TOKEN = "token-impresora-0001"
BUSINESS = "negocio-1"
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Business-Id": BUSINESS}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PRINTER_TOKEN", TOKEN)
    monkeypatch.setenv("BUSINESS_ID", BUSINESS)
    return TestClient(main.app)


def test_version(client):
    assert client.get("/version").json() == {"version": main.current_version()}


@pytest.mark.parametrize("ruta", ["/print", "/update", "/wifi"])
@pytest.mark.parametrize("headers", [
    {},
    {"X-Business-Id": BUSINESS},
    {"Authorization": "Bearer otro-token", "X-Business-Id": BUSINESS},
    {"Authorization": TOKEN, "X-Business-Id": BUSINESS},  # sin "Bearer"
    {"Authorization": "Bearer tökën".encode("latin-1"), "X-Business-Id": BUSINESS},  # no ASCII: 401, no 500
])
def test_token_invalido_da_401(client, ruta, headers):
    assert client.post(ruta, json={"text": "x"}, headers=headers).status_code == 401


@pytest.mark.parametrize("ruta", ["/print", "/update", "/wifi"])
def test_otro_negocio_da_403(client, ruta):
    headers = {**AUTH, "X-Business-Id": "negocio-2"}
    assert client.post(ruta, json={"text": "x"}, headers=headers).status_code == 403


def test_sin_configurar_rechaza_todo(client, monkeypatch):
    monkeypatch.delenv("PRINTER_TOKEN")
    assert client.post("/print", json={"text": "x"}, headers={"Authorization": "Bearer ", "X-Business-Id": BUSINESS}).status_code == 401
    monkeypatch.setenv("PRINTER_TOKEN", TOKEN)
    monkeypatch.delenv("BUSINESS_ID")
    assert client.post("/print", json={"text": "x"}, headers={**AUTH, "X-Business-Id": ""}).status_code == 403


def test_imprime_con_el_payload_del_backend_principal(client, monkeypatch):
    impreso = []
    monkeypatch.setattr(hardware, "check_printer", lambda: True)
    monkeypatch.setattr(hardware, "print_text", lambda text, cut, copies: impreso.append((text, cut, copies)))
    body = {"text": "COMANDA\nPago: EFECTIVO", "payment_method": "efectivo", "copies": 2, "paper_width": "80mm"}
    r = client.post("/print", json=body, headers=AUTH)
    assert r.status_code == 200
    assert impreso == [("COMANDA\nPago: EFECTIVO", True, 2)]


def test_imprime_sin_impresora(client, monkeypatch):
    monkeypatch.setattr(hardware, "check_printer", lambda: False)
    assert client.post("/print", json={"text": "x"}, headers=AUTH).status_code == 503


@pytest.mark.parametrize("body", [{"text": ""}, {"text": "x", "copies": 9}, {"text": "x", "paper_width": "100mm"}])
def test_payload_invalido(client, body):
    assert client.post("/print", json=body, headers=AUTH).status_code == 422


def _fake_git(heads, pull_rc=0, diff=""):
    """Simula git: rev-parse devuelve heads en orden."""
    heads = iter(heads)
    def run(*cmd):
        out, rc = "", 0
        if cmd[:2] == ("git", "rev-parse"):
            out = next(heads)
        elif cmd[:2] == ("git", "pull"):
            rc = pull_rc
        elif cmd[:2] == ("git", "diff"):
            out = diff
        return subprocess.CompletedProcess(cmd, rc, out, "error de git" if rc else "")
    return run


def test_update_sin_cambios_no_reinicia(client, monkeypatch):
    reinicios = []
    monkeypatch.setattr(main, "_run", _fake_git(["abc", "abc"]))
    monkeypatch.setattr(main, "_restart", lambda: reinicios.append(1))
    r = client.post("/update", headers=AUTH)
    assert r.json()["updated"] is False
    assert reinicios == []


def test_update_con_cambios_reinicia(client, monkeypatch):
    reinicios = []
    monkeypatch.setattr(main, "_run", _fake_git(["abc", "def"], diff="app/main.py"))
    monkeypatch.setattr(main, "_restart", lambda: reinicios.append(1))
    r = client.post("/update", headers=AUTH)
    assert r.json()["updated"] is True
    assert r.json()["commit"] == "def"
    assert reinicios == [1]


def test_update_con_lock_nuevo_sincroniza(client, monkeypatch):
    comandos = []
    fake = _fake_git(["abc", "def"], diff="uv.lock")
    monkeypatch.setattr(main, "_run", lambda *cmd: comandos.append(cmd) or fake(*cmd))
    monkeypatch.setattr(main, "_restart", lambda: None)
    assert client.post("/update", headers=AUTH).json()["updated"] is True
    assert ("uv", "sync", "--frozen", "--no-dev") in comandos


def test_update_pull_falla(client, monkeypatch):
    monkeypatch.setattr(main, "_run", _fake_git(["abc"], pull_rc=1))
    r = client.post("/update", headers=AUTH)
    assert r.status_code == 500
    assert "error de git" in r.json()["detail"]


WIFI = {"ssid": "Edy Gourmet", "password": "clave-del-local"}


def test_wifi_guarda_el_perfil_con_prioridad(client, monkeypatch):
    comandos = []
    monkeypatch.setattr(main, "_run", lambda *cmd: comandos.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    r = client.post("/wifi", json=WIFI, headers=AUTH)
    assert r.json() == {"saved": True, "ssid": "Edy Gourmet"}
    borrar, agregar = comandos
    assert borrar == ("sudo", "-n", "nmcli", "connection", "delete", "id", "wifi-negocio")
    assert agregar[:6] == ("sudo", "-n", "nmcli", "connection", "add", "type")
    assert ("ssid", "Edy Gourmet") == agregar[agregar.index("ssid"):agregar.index("ssid") + 2]
    assert "clave-del-local" in agregar and agregar[-2:] == ("connection.autoconnect-priority", "10")


@pytest.mark.parametrize("body", [
    {"ssid": "", "password": "clave-del-local"},
    {"ssid": "x" * 33, "password": "clave-del-local"},
    {"ssid": "Edy", "password": "corta"},
])
def test_wifi_invalido_no_devuelve_la_clave(client, monkeypatch, body):
    monkeypatch.setattr(main, "_run", lambda *cmd: pytest.fail("no debe llamar a nmcli"))
    r = client.post("/wifi", json=body, headers=AUTH)
    assert r.status_code == 422
    assert body["password"] not in r.text


def test_wifi_nmcli_falla(client, monkeypatch):
    monkeypatch.setattr(main, "_run", lambda *cmd: subprocess.CompletedProcess(cmd, 1, "", "sudo: a password is required"))
    r = client.post("/wifi", json=WIFI, headers=AUTH)
    assert r.status_code == 500
    assert "password is required" in r.json()["detail"]
