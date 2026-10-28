"""El proxy del panel: solo entra quien trae un JWT válido de Cloudflare
Access; pone la key y el token del worker (el navegador no los ve), no deja
llegar a los jobs y firma el cambio del bot con el correo verificado."""

import datetime
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from google.auth import crypt, jwt

from app.services import panel_entry

TEAM = "j2ys.cloudflareaccess.com"
AUD = "aud-del-panel"


def _key_and_cert():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cloudflare-access")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return pem_key.decode(), cert.public_bytes(serialization.Encoding.PEM).decode()


PEM_KEY, PEM_CERT = _key_and_cert()


def _token(**claims) -> str:
    now = int(time.time())
    payload = {"aud": [AUD], "iss": f"https://{TEAM}", "email": "dev@3b-technology.com", "iat": now, "exp": now + 300, **claims}
    return jwt.encode(crypt.RSASigner.from_string(PEM_KEY, key_id="k1"), payload).decode()


@pytest.fixture
def proxy(monkeypatch):
    monkeypatch.setattr(panel_entry, "WORKER_URL", "https://worker.test")
    monkeypatch.setattr(panel_entry, "API_KEY", "clave-admin")
    monkeypatch.setattr(panel_entry, "CF_TEAM_DOMAIN", TEAM)
    monkeypatch.setattr(panel_entry, "CF_AUD", AUD)
    monkeypatch.setattr(panel_entry, "_access_certs", lambda: {"k1": PEM_CERT})
    monkeypatch.setattr(panel_entry, "_id_token", lambda: "token-oidc")
    send = AsyncMock(return_value=httpx.Response(200, json={"ok": True}))
    monkeypatch.setattr(panel_entry, "_send", send)
    return TestClient(panel_entry.app), send


def _auth(token=None):
    return {"Cf-Access-Jwt-Assertion": token or _token()}


def test_requests_reach_the_worker_with_key_and_token(proxy):
    client, send = proxy

    response = client.patch(
        "/api/v1/admin/configurations/b1?x=1", json={"tono": "formal"}, headers={**_auth(), "X-API-Key": "del-navegador"}
    )

    assert response.status_code == 200 and response.json() == {"ok": True}
    method, url, headers, content = send.await_args.args
    assert (method, url) == ("PATCH", "https://worker.test/api/v1/admin/configurations/b1?x=1")
    assert headers["X-API-Key"] == "clave-admin" and headers["Authorization"] == "Bearer token-oidc"
    assert json.loads(content) == {"tono": "formal"}


@pytest.mark.parametrize(
    "token",
    [
        "",
        "basura",
        _token(aud=["otra-aplicacion"]),
        _token(iss="https://otro.cloudflareaccess.com"),
        _token(exp=int(time.time()) - 600, iat=int(time.time()) - 900),
        _token(email=None),
    ],
)
def test_without_a_valid_cloudflare_token_nothing_gets_through(proxy, token):
    client, send = proxy

    for path in ("/api/v1/admin/businesses", "/configuracion.html", "/"):
        assert client.get(path, headers={"Cf-Access-Jwt-Assertion": token}).status_code == 403
    send.assert_not_awaited()


def test_the_cloudflare_cookie_also_works(proxy):
    client, _ = proxy
    client.cookies.set("CF_Authorization", _token())

    assert client.get("/api/v1/admin/businesses").status_code == 200


def test_without_cloudflare_configured_nobody_gets_in(proxy, monkeypatch):
    client, _ = proxy
    monkeypatch.setattr(panel_entry, "CF_AUD", "")

    assert client.get("/api/v1/admin/businesses", headers=_auth()).status_code == 403


def test_jobs_are_not_reachable_from_the_panel(proxy):
    client, send = proxy

    assert client.post("/api/v1/admin/jobs/delivery-sweep", headers=_auth()).status_code == 404
    send.assert_not_awaited()


def test_the_bot_switch_is_signed_with_the_verified_email(proxy):
    client, send = proxy

    client.put(
        "/api/v1/admin/businesses/b1/bot-status",
        json={"status": "inactive", "actor": "quien-sea"},
        headers=_auth(_token(email="samuel@3b-technology.com")),
    )

    assert json.loads(send.await_args.args[3])["actor"] == "samuel@3b-technology.com"


def test_the_front_is_always_revalidated(tmp_path):
    """Regresión (2026-10-25): sin esto el navegador guardaba el JS 4 h y tras
    un despliegue la configuración no cargaba."""
    (tmp_path / "configuracion.js").write_text("// front")
    client = TestClient(panel_entry.NoCacheStaticFiles(directory=tmp_path, html=True))

    first = client.get("/configuracion.js")
    again = client.get("/configuracion.js", headers={"If-None-Match": first.headers["etag"]})

    assert first.headers["cache-control"] == "no-cache"
    assert again.status_code == 304 and again.headers["cache-control"] == "no-cache"
