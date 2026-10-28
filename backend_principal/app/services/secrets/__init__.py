"""Secretos por negocio en Secret Manager.

Firestore guarda solo la REFERENCIA (`projects/<p>/secrets/<id>`, sin
versión); el valor vive acá y se lee siempre en `latest`, así que rotar es
agregar una versión nueva. Nombres: `<tipo>--<businessId>`, con el tipo
primero para poder dar acceso por prefijo con una condición de IAM (el
webhook solo lee `kapso-webhook-secret--*`, el worker `kapso-api-key--*`;
ver SECURITY.md).

Autentica con ADC: la cuenta de servicio de Cloud Run en producción,
`gcloud auth application-default login` en local.

Nunca se loguea un valor, ni parcial. Las llamadas son bloqueantes (gRPC);
con la caché, solo la primera lectura de cada secreto por proceso paga la
red.
"""

import logging
import threading
import time
from typing import Optional

from app.config import firestore_settings, secrets_settings

logger = logging.getLogger("secrets")

KAPSO_API_KEY = "kapso-api-key"
KAPSO_WEBHOOK_SECRET = "kapso-webhook-secret"
PRINTER_TOKEN = "printer-token"

_TIMEOUT_SECONDS = 5
_client = None
_lock = threading.Lock()
# (ref, con_anterior) -> (valores [latest, anterior?], vence_en)
_cache: dict[tuple[str, bool], tuple[list[str], float]] = {}


def _project() -> str:
    return secrets_settings.SECRETS_PROJECT or firestore_settings.PROJECT_ID


def _get_client():
    # Import diferido: el webhook importa este módulo y no debe cargar gRPC
    # de Secret Manager hasta el primer evento que lo necesite.
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                from google.cloud import secretmanager

                _client = secretmanager.SecretManagerServiceClient()
    return _client


def secret_ref(kind: str, business_id: str) -> str:
    """Nombre del recurso del secreto `kind` del negocio (lo que se guarda en
    Firestore)."""
    return f"projects/{_project()}/secrets/{kind}--{business_id}"


def _versions(ref: str, with_previous: bool) -> list[str]:
    """[latest, anterior] -- la anterior solo si se pide, existe y está habilitada.
    Se pide por número (latest - 1) y no listando versiones: listar exige
    `versions.list`, que secretAccessor no trae."""
    from google.api_core.exceptions import FailedPrecondition, NotFound

    client = _get_client()
    # Timeout corto: el worker es un solo proceso y esto bloquea el event loop.
    latest = client.access_secret_version(name=f"{ref}/versions/latest", timeout=_TIMEOUT_SECONDS)
    values = [latest.payload.data.decode("utf-8")]
    number = int(latest.name.rsplit("/", 1)[-1])
    if with_previous and number > 1:
        try:
            previous = client.access_secret_version(name=f"{ref}/versions/{number - 1}", timeout=_TIMEOUT_SECONDS)
            values.append(previous.payload.data.decode("utf-8"))
        except (NotFound, FailedPrecondition):
            pass  # destruida o deshabilitada: ya no vale
    return values


def _cached_versions(ref: str, with_previous: bool) -> list[str]:
    key = (ref, with_previous)
    cached = _cache.get(key)
    if cached is not None and cached[1] > time.monotonic():
        return cached[0]
    values = _versions(ref, with_previous)
    _cache[key] = (values, time.monotonic() + secrets_settings.SECRETS_CACHE_SECONDS)
    return values


def get_secret_versions(ref: str) -> list[str]:
    """Valores vigentes del secreto: [latest] o [latest, anterior]. La
    anterior permite rotar la firma del webhook sin rechazar los eventos
    que Kapso firmó con el secreto viejo mientras se actualiza."""
    return _cached_versions(ref, with_previous=True)


def get_secret(ref: str) -> str:
    """Valor `latest` del secreto (cacheado). Lanza si no existe o no hay
    permiso: el que llama decide si es fatal."""
    return _cached_versions(ref, with_previous=False)[0]


def invalidate(ref: str) -> None:
    """Olvida el valor cacheado (ej. Kapso respondió 401: quizá rotaron)."""
    _cache.pop((ref, False), None)
    _cache.pop((ref, True), None)


def clear_cache() -> None:
    _cache.clear()


def put_secret(kind: str, business_id: str, value: str) -> str:
    """Crea el secreto si no existe y le agrega `value` como versión nueva.
    Devuelve la referencia para guardar en Firestore."""
    from google.api_core.exceptions import AlreadyExists

    ref = secret_ref(kind, business_id)
    client = _get_client()
    try:
        client.create_secret(
            request={
                "parent": f"projects/{_project()}",
                "secret_id": ref.rsplit("/", 1)[-1],
                "secret": {"replication": {"automatic": {}}, "labels": {"business_id": business_id.lower()}},
            }
        )
    except AlreadyExists:
        pass
    client.add_secret_version(request={"parent": ref, "payload": {"data": value.encode("utf-8")}})
    invalidate(ref)
    logger.info("Secreto %s actualizado (negocio %s)", kind, business_id)
    return ref


def get_secret_or_none(ref: Optional[str]) -> Optional[str]:
    """Como get_secret pero sin lanzar: None si no hay ref o falló la
    lectura (queda el error en el log, sin el valor)."""
    if not ref:
        return None
    try:
        return get_secret(ref)
    except Exception:
        logger.exception("No se pudo leer el secreto %s", ref)
        return None
