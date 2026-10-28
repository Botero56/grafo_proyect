import os
import secrets
import signal
import subprocess
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app import hardware, telemetry

ROOT = Path(__file__).resolve().parent.parent
def current_version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


@asynccontextmanager
async def lifespan(_app):
    telemetry.start_heartbeat_loop(current_version())
    yield


app = FastAPI(title="backend_rasperry", lifespan=lifespan)


def require_auth(authorization: str = Header(default=""), x_business_id: str = Header(default="")) -> None:
    """Contrato del backend principal (j2ys docs/RUNBOOK.md §4):
    `Authorization: Bearer <token del negocio>` y `X-Business-Id`.
    401 si el token no coincide, 403 si el negocio no es el de esta Raspberry."""
    expected = os.environ.get("PRINTER_TOKEN", "").encode()
    scheme, _, sent = authorization.partition(" ")
    if not expected or scheme != "Bearer" or not secrets.compare_digest(sent.strip().encode(), expected):
        raise HTTPException(401, "token inválido")
    business_id = os.environ.get("BUSINESS_ID", "")
    if not business_id or x_business_id != business_id:
        raise HTTPException(403, "esta impresora no es de ese negocio")


def _run(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)


def _restart() -> None:
    # systemd (Restart=always) vuelve a levantar el proceso con el código nuevo.
    os.kill(os.getpid(), signal.SIGTERM)


class PrintJob(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)
    copies: int = Field(default=1, ge=1, le=5)
    cut: bool = True
    # Los manda el backend principal. El texto ya trae la línea de pago y viene
    # armado para el ancho del papel, así que aquí solo se aceptan.
    payment_method: str | None = None
    paper_width: Literal["58mm", "80mm"] | None = None


@app.get("/version")
def get_version():
    return {"version": current_version()}


@app.post("/print", dependencies=[Depends(require_auth)])
def post_print(job: PrintJob):
    if not hardware.check_printer():
        telemetry.record_print(False, "impresora no conectada")
        raise HTTPException(503, "impresora no conectada")
    try:
        hardware.print_text(job.text, job.cut, job.copies)
    except Exception as exc:
        telemetry.record_print(False, f"error al imprimir: {exc}")
        raise HTTPException(500, f"error al imprimir: {exc}") from exc
    # Enviado al USB, no prueba que salió papel.
    telemetry.record_print(True)
    return {"ok": True}


@app.get("/health", dependencies=[Depends(require_auth)])
def get_health():
    """Mismo estado que el heartbeat, pedido en el momento (botón Comprobar conexión)."""
    return telemetry.snapshot(current_version())


class Wifi(BaseModel):
    # Sin Field(min_length=...) a propósito: un 422 de pydantic devuelve el
    # valor recibido, y aquí ese valor es la clave del Wi-Fi.
    ssid: str
    password: str


# Un solo perfil para el Wi-Fi del negocio: guardarlo otra vez lo reemplaza.
WIFI_CONNECTION = "wifi-negocio"


@app.post("/wifi", dependencies=[Depends(require_auth)])
def post_wifi(wifi: Wifi):
    """Guarda el Wi-Fi del negocio sin cambiarse a él. La Raspberry sigue en la
    red actual (el hotspot del equipo); al apagarlo, NetworkManager entra solo
    al del negocio, que tiene más prioridad. Si esa clave falla, vuelve al hotspot."""
    if not 1 <= len(wifi.ssid.encode()) <= 32:
        raise HTTPException(422, "El nombre del Wi-Fi debe tener entre 1 y 32 bytes")
    if not 8 <= len(wifi.password) <= 63:
        raise HTTPException(422, "La clave del Wi-Fi debe tener entre 8 y 63 caracteres")
    _run("sudo", "-n", "nmcli", "connection", "delete", "id", WIFI_CONNECTION)  # puede no existir
    # ponytail: la clave pasa por argv (visible en `ps` un instante); usar un archivo temporal si la Raspberry tiene más usuarios.
    add = _run(
        "sudo", "-n", "nmcli", "connection", "add", "type", "wifi", "con-name", WIFI_CONNECTION,
        "ssid", wifi.ssid, "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", wifi.password,
        "connection.autoconnect", "yes", "connection.autoconnect-priority", "10",
    )
    if add.returncode != 0:
        raise HTTPException(500, f"nmcli falló: {add.stderr.strip()[:200]}")
    return {"saved": True, "ssid": wifi.ssid}


@app.post("/update", dependencies=[Depends(require_auth)])
def post_update(background: BackgroundTasks):
    old_version = current_version()
    before = _run("git", "rev-parse", "HEAD").stdout.strip()

    pull = _run("git", "pull", "--ff-only")
    if pull.returncode != 0:
        raise HTTPException(500, pull.stderr.strip() or "git pull falló")

    after = _run("git", "rev-parse", "HEAD").stdout.strip()
    if before == after:
        return {"updated": False, "version": old_version}

    changed = _run("git", "diff", "--name-only", before, after).stdout.split()
    if {"pyproject.toml", "uv.lock"} & set(changed):
        sync = _run("uv", "sync", "--frozen", "--no-dev")
        if sync.returncode != 0:
            raise HTTPException(500, f"uv sync falló: {sync.stderr.strip()}")

    background.add_task(_restart)  # corre después de enviar la respuesta
    return {"updated": True, "from": old_version, "to": current_version(), "commit": after}
