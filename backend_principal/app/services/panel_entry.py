"""Panel interno (servicio `j2ys-panel`): sirve el front de configuración y
reenvía `/api/v1/admin/*` al worker privado.

El navegador nunca ve la API key ni el token: los pone este servicio. Quién
entra lo decide Cloudflare Access delante de panel.3b-technology.com: cada
request trae su JWT firmado (`Cf-Access-Jwt-Assertion`) y aquí se verifica
firma, audiencia y emisor. Sin un JWT válido todo responde 403, también
quien llegue directo por la URL run.app. Ver docs/RUNBOOK.md §10.

Lo mínimo a propósito: no lee Firestore ni conoce los modelos; solo reenvía.
"""

import json
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

WORKER_URL = os.environ.get("WORKER_URL", "").rstrip("/")
API_KEY = os.environ.get("PANEL_API_KEY", "")
STATIC_DIR = Path(os.environ.get("PANEL_STATIC_DIR", "panel_static"))
# Cloudflare Zero Trust: "<equipo>.cloudflareaccess.com" y el AUD tag de la
# aplicación. Vacíos = nadie entra (falla cerrado).
CF_TEAM_DOMAIN = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip().removeprefix("https://").rstrip("/")
CF_AUD = os.environ.get("CF_ACCESS_AUD", "").strip()
_CERTS_TTL_SECONDS = 60 * 60
# Los jobs son solo para Cloud Scheduler.
_BLOCKED_PREFIXES = ("jobs/",)
_TOKEN_TTL_SECONDS = 50 * 60  # los ID tokens de Google duran 1 h

_token: tuple[str, float] = ("", 0.0)
_certs: tuple[dict, float] = ({}, 0.0)

app = FastAPI(title="Panel 3b-technology", docs_url=None, redoc_url=None, openapi_url=None)


def _access_certs() -> dict:
    """{kid: certificado PEM} con que firma Cloudflare Access (rota sus llaves).
    ponytail: lectura bloqueante una vez por hora dentro del middleware."""
    global _certs
    if time.monotonic() >= _certs[1]:
        data = httpx.get(f"https://{CF_TEAM_DOMAIN}/cdn-cgi/access/certs", timeout=10).json()
        _certs = ({c["kid"]: c["cert"] for c in data["public_certs"]}, time.monotonic() + _CERTS_TTL_SECONDS)
    return _certs[0]


def verified_email(token: str) -> str | None:
    """Correo del JWT de Cloudflare Access, o None si no es válido."""
    if not (token and CF_TEAM_DOMAIN and CF_AUD):
        return None
    from google.auth import jwt

    try:
        # Firma y vencimiento. La audiencia se revisa aparte: Cloudflare la
        # manda como lista y google-auth solo la compara como texto.
        claims = jwt.decode(token, certs=_access_certs(), audience=None)
    except Exception:
        return None
    aud = claims.get("aud")
    if CF_AUD not in (aud if isinstance(aud, list) else [aud]):
        return None
    if claims.get("iss") != f"https://{CF_TEAM_DOMAIN}" or not claims.get("email"):
        return None
    return claims["email"]


@app.middleware("http")
async def require_cloudflare_access(request: Request, call_next):
    token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization", "")
    email = verified_email(token)
    if email is None:
        return PlainTextResponse("Acceso solo por el panel de 3b-technology.", status_code=403)
    request.state.email = email
    return await call_next(request)


def _id_token() -> str:
    """Token OIDC de la cuenta del servicio para invocar al worker."""
    global _token
    if time.monotonic() >= _token[1]:
        import google.auth.transport.requests
        import google.oauth2.id_token

        value = google.oauth2.id_token.fetch_id_token(google.auth.transport.requests.Request(), WORKER_URL)
        _token = (value, time.monotonic() + _TOKEN_TTL_SECONDS)
    return _token[0]


async def _send(method: str, url: str, headers: dict, content: bytes) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.request(method, url, headers=headers, content=content)


@app.api_route("/api/v1/admin/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def forward(path: str, request: Request) -> Response:
    if not WORKER_URL or not API_KEY or path.startswith(_BLOCKED_PREFIXES):
        return Response(status_code=404)
    content = await request.body()
    if request.method == "PUT" and path.endswith(("/bot-status", "/plan")) and content:
        # Quién apagó el bot o cambió el plan: el correo que verificó
        # Cloudflare, no lo que diga el navegador.
        body = json.loads(content)
        body["actor"] = request.state.email
        content = json.dumps(body).encode()
    url = f"{WORKER_URL}/api/v1/admin/{path}"
    if request.url.query:
        url += f"?{request.url.query}"
    headers = {
        "Authorization": f"Bearer {_id_token()}",
        "X-API-Key": API_KEY,
        "Content-Type": request.headers.get("content-type", "application/json"),
    }
    upstream = await _send(request.method, url, headers, content)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


@app.get("/")
def home() -> RedirectResponse:
    return RedirectResponse("/configuracion.html")


class NoCacheStaticFiles(StaticFiles):
    """Sin Cache-Control, Cloudflare le dice al navegador que guarde el front
    4 h: tras un despliegue mezclaba HTML nuevo con JS viejo y la página se
    rompía. `no-cache` = revalidar siempre (304 con el ETag si no cambió)."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


if STATIC_DIR.is_dir():
    app.mount("/", NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="panel")
