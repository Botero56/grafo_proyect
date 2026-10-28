"""El worker verifica el token OIDC con el que Cloud Tasks lo invoca.

Cloud Run privado ya exige un token de Google válido (IAM); esto agrega que
sea de NUESTRA cuenta de servicio y para NUESTRO worker. Así, otra
identidad con `run.invoker` no puede inyectar tareas.

Se salta con el emulador local (CLOUD_TASKS_EMULATOR_HOST), que no firma.
"""

import logging
import threading
import time

from fastapi import HTTPException, Request

from app.config import cloud_tasks_settings

logger = logging.getLogger("whatsapp-worker")

_CERTS_TTL_SECONDS = 3600


class _CachedCertsRequest:
    """Transporte de google-auth que guarda los certificados públicos de
    Google una hora: sin esto, cada tarea los descargaría de nuevo."""

    def __init__(self) -> None:
        import google.auth.transport.requests

        self._inner = google.auth.transport.requests.Request()
        self._cache: dict[str, tuple[object, float]] = {}

    def __call__(self, url, method="GET", **kwargs):
        hit = self._cache.get(url)
        if hit is not None and hit[1] > time.monotonic():
            return hit[0]
        response = self._inner(url, method=method, **kwargs)
        if response.status == 200:
            self._cache[url] = (response, time.monotonic() + _CERTS_TTL_SECONDS)
        return response


_transport = None
_lock = threading.Lock()


def _get_transport():
    global _transport
    if _transport is None:
        with _lock:
            if _transport is None:
                _transport = _CachedCertsRequest()
    return _transport


def require_task_oidc(request: Request) -> None:
    """Dependencia de FastAPI para /tasks/*. 401 = Cloud Tasks reintenta,
    así una configuración rota no pierde tareas."""
    if cloud_tasks_settings.CLOUD_TASKS_EMULATOR_HOST:
        return
    from google.oauth2 import id_token

    expected_emails = cloud_tasks_settings.task_accepted_emails
    worker_url = cloud_tasks_settings.WORKER_URL.rstrip("/")
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not expected_emails or not token:
        logger.warning("Tarea sin token OIDC (o sin WORKER_SERVICE_ACCOUNT_EMAIL configurado)",
                       extra={"fields": {"security_event": "task_without_oidc"}})
        raise HTTPException(status_code=401)
    try:
        # La audiencia es WORKER_URL (ver cloud_tasks_client._oidc); las tareas
        # encoladas antes de fijarla traen la URL completa de la ruta.
        claims = id_token.verify_token(
            token, _get_transport(), audience=[worker_url, f"{worker_url}{request.url.path}"]
        )
    except Exception:
        logger.warning("Token OIDC inválido en una tarea", extra={"fields": {"security_event": "task_bad_oidc"}})
        raise HTTPException(status_code=401)
    if claims.get("email") not in expected_emails or not claims.get("email_verified"):
        logger.warning("Tarea firmada por otra cuenta", extra={"fields": {"security_event": "task_wrong_identity"}})
        raise HTTPException(status_code=401)
