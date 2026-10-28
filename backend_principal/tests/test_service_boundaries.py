"""Qué expone cada servicio y qué carga.

Son dos servicios con su propio Dockerfile: el webhook (público) y el worker
(privado). Si una ruta termina en el servicio equivocado, o el webhook
empieza a importar el stack pesado, la imagen del webhook deja de arrancar
(no tiene instalado el grupo `worker`) o la API admin queda expuesta."""

import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.services import webhook_entry, worker_entry

webhook = TestClient(webhook_entry.app)
worker = TestClient(worker_entry.app)

# Se prueba por HTTP (404 = la ruta no existe en ese servicio) y no
# recorriendo app.routes: desde FastAPI 0.14x los routers incluidos quedan
# envueltos y no exponen `.path`.
_ADMIN_JOBS = [
    "/api/v1/admin/jobs/reactivate-bots",
    "/api/v1/admin/jobs/expire-pending-daily-menus",
    "/api/v1/admin/jobs/remind-missing-daily-menus",
]
_TASKS = ["/tasks/process-event", "/tasks/flush-conversation"]


def test_the_webhook_exposes_only_the_public_routes():
    assert webhook.get("/webhook").status_code != 404
    # El onboarding propio (Embedded Signup) se retiró: los números van por Kapso.
    assert webhook.get("/onboarding").status_code == 404
    assert webhook.get("/").status_code != 404

    for path in _TASKS + _ADMIN_JOBS:
        assert webhook.post(path).status_code == 404, path
    assert webhook.get("/api/v1/admin/businesses/").status_code == 404


def test_the_worker_serves_the_admin_api_behind_the_api_key():
    # Cloud Scheduler apunta a estos jobs: antes solo existían en el
    # monolito, que no corría en producción. Sin X-API-Key: 422, no 404.
    for path in _ADMIN_JOBS:
        assert worker.post(path).status_code == 422, path
    assert worker.post(_ADMIN_JOBS[0], headers={"X-API-Key": "incorrecta"}).status_code == 401

    assert worker.post("/webhook").status_code == 404
    assert worker.get("/onboarding").status_code == 404


def test_ingress_does_not_load_the_heavy_stack():
    """El valor de separar el webhook es que arranque sin el peso del
    worker, y su imagen ni siquiera instala esas dependencias. Se mide sobre
    los módulos REALMENTE cargados, en un proceso limpio: si mañana alguien
    importa el agente o Firestore desde webhook_entry (directo o de rebote),
    este test lo detecta."""
    probe = (
        "import sys; import app.services.webhook_entry;"
        "heavy=[m for m in ('google.adk','fireo','google.cloud.firestore','pandas',"
        "'matplotlib','app.ai.core.agent_runner','app.ai.vertical_registry') if m in sys.modules];"
        "print(','.join(heavy))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"el ingress carga: {result.stdout.strip()}"


def test_without_an_admin_key_configured_the_admin_api_rejects_everything(monkeypatch):
    from app.config import admin_api_settings

    monkeypatch.setattr(admin_api_settings, "API_KEY", "")
    assert worker.get("/api/v1/admin/businesses/x", headers={"X-API-Key": ""}).status_code == 401
