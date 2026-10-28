"""Cliente de Cloud Tasks: encola UN evento para que lo procese el worker.

Lo usan `app.services.webhook_entry` (encola cada evento), el worker
(agenda el flush).

`CloudTasksClient` usa gRPC y es seguro entre hilos: se crea una sola vez
por proceso. Todas sus llamadas son síncronas (bloqueantes), así que quien
las use debe correrlas en un hilo (`run_in_threadpool`), igual que el resto
del I/O síncrono del proyecto.

En dev apunta al emulador (github.com/aertje/cloud-tasks-emulator) vía
`CLOUD_TASKS_EMULATOR_HOST`; en producción usa el servicio real de GCP y
firma la invocación al worker con un token OIDC de
`WORKER_SERVICE_ACCOUNT_EMAIL` (el Cloud Run privado es quien exige ese
token; acá solo se le pide a Cloud Tasks que lo adjunte).
"""

import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

from app.config import cloud_tasks_settings, firestore_settings

logger = logging.getLogger("whatsapp-webhook")

_client: Optional[tasks_v2.CloudTasksClient] = None
_ensured_queues: set[str] = set()
_lock = threading.Lock()


def _project() -> str:
    return cloud_tasks_settings.CLOUD_TASKS_PROJECT or firestore_settings.PROJECT_ID


def get_client() -> tasks_v2.CloudTasksClient:
    """Cliente del proceso, creándolo (y apuntándolo al emulador si aplica)
    la primera vez."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                if cloud_tasks_settings.CLOUD_TASKS_EMULATOR_HOST:
                    import grpc
                    from google.cloud.tasks_v2.services.cloud_tasks.transports import (
                        CloudTasksGrpcTransport,
                    )

                    channel = grpc.insecure_channel(cloud_tasks_settings.CLOUD_TASKS_EMULATOR_HOST)
                    _client = tasks_v2.CloudTasksClient(
                        transport=CloudTasksGrpcTransport(channel=channel)
                    )
                    logger.info(
                        "Cloud Tasks: usando el emulador en %s",
                        cloud_tasks_settings.CLOUD_TASKS_EMULATOR_HOST,
                    )
                else:
                    _client = tasks_v2.CloudTasksClient()
    return _client


def _queue_path(queue: Optional[str] = None) -> str:
    return get_client().queue_path(
        _project(),
        cloud_tasks_settings.CLOUD_TASKS_LOCATION,
        queue or cloud_tasks_settings.CLOUD_TASKS_QUEUE,
    )


def _ensure_queue_in_emulator(queue_path: str) -> None:
    """El emulador no trae colas preexistentes salvo que se declaren al
    arrancarlo; se crea perezosamente (una vez por cola) para no depender de
    esa configuración. Cloud Tasks real NO se toca: la cola de producción la
    crea el despliegue, no la app."""
    if not cloud_tasks_settings.CLOUD_TASKS_EMULATOR_HOST:
        return
    if queue_path in _ensured_queues:
        return
    with _lock:
        if queue_path in _ensured_queues:
            return
        parent = (
            f"projects/{_project()}/locations/{cloud_tasks_settings.CLOUD_TASKS_LOCATION}"
        )
        try:
            get_client().create_queue(request={"parent": parent, "queue": {"name": queue_path}})
        except AlreadyExists:
            pass
        _ensured_queues.add(queue_path)


def _task_name(queue_path: str, event_id: str) -> str:
    # El wamid puede traer '/' u otros caracteres que Cloud Tasks no acepta
    # en el nombre de una tarea: se deriva un id determinista y seguro.
    safe = hashlib.sha256(event_id.encode()).hexdigest()
    return f"{queue_path}/tasks/{safe}"


def _oidc() -> dict:
    return (
        {
            "oidc_token": {
                "service_account_email": cloud_tasks_settings.task_signer_email,
                # El worker exige esta audiencia (ver tasks/task_auth.py).
                "audience": cloud_tasks_settings.WORKER_URL.rstrip("/"),
            }
        }
        if cloud_tasks_settings.WORKER_SERVICE_ACCOUNT_EMAIL
        else {}
    )


def _http_request(route: str, payload: dict) -> dict:
    return {
        "http_method": tasks_v2.HttpMethod.POST,
        "url": f"{cloud_tasks_settings.WORKER_URL.rstrip('/')}{route}",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload).encode("utf-8"),
        **_oidc(),
    }


def _create_named_task(
    queue: Optional[str], route: str, payload: dict, task_id: str, delay_seconds: Optional[float] = None
) -> bool:
    """Tarea con nombre determinista (deduplicada por Cloud Tasks) hacia
    `route` del worker, opcionalmente para dentro de `delay_seconds`. True
    si se creó, False si ya existía."""
    queue_path = _queue_path(queue)
    _ensure_queue_in_emulator(queue_path)
    task = {"name": _task_name(queue_path, task_id), "http_request": _http_request(route, payload)}
    if delay_seconds:
        schedule_time = timestamp_pb2.Timestamp()
        schedule_time.FromDatetime(datetime.now(timezone.utc) + timedelta(seconds=delay_seconds))
        task["schedule_time"] = schedule_time
    request = {"parent": queue_path, "task": task}
    try:
        get_client().create_task(request=request)
        return True
    except AlreadyExists:
        return False


def _envelope(payload: dict, business_id: Optional[str]) -> dict:
    """Cuerpo de /tasks/process-event. `business_id` lo pone el webhook por
    tenant (ya resolvió el negocio y verificó la firma con SU secreto);
    None = webhook legado, el worker lo resuelve por el número."""
    return {"business_id": business_id, "event": payload}


def enqueue_sync_event(payload: dict, event_id: str, business_id: Optional[str] = None) -> bool:
    """Encola un webhook `history`/`smb_app_state_sync` en la cola de
    historial (CLOUD_TASKS_HISTORY_QUEUE), hacia la MISMA ruta del worker
    que los mensajes. Va aparte porque llega en ráfaga y la cola de
    historial tiene límites de velocidad bajos: así no atrasa los mensajes
    nuevos ni satura Firestore. `event_id` = webhook_payload.content_event_id.

    Bloqueante: llamar con `run_in_threadpool`."""
    return _create_named_task(
        cloud_tasks_settings.CLOUD_TASKS_HISTORY_QUEUE,
        "/tasks/process-event",
        _envelope(payload, business_id),
        event_id,
    )


def enqueue_event(payload: dict, event_id: str, business_id: Optional[str] = None) -> bool:
    """Encola UN evento ya separado (ver webhook_payload.split_events).

    El nombre de la tarea deriva del wamid: si Meta reentrega el mismo
    webhook (una request HTTP nueva, no un reintento de Cloud Tasks),
    `create_task` con el mismo nombre falla con `AlreadyExists` y se ignora.
    Eso deduplica a nivel de cola, ANTES de que el worker toque Firestore, y
    no reemplaza la idempotencia de negocio (`WebhookEvents`), que cubre los
    reintentos de la propia cola si el worker falla a mitad de camino.

    Devuelve True si se encoló, False si ya existía. Bloqueante: llamar con
    `run_in_threadpool`.
    """
    return _create_named_task(None, "/tasks/process-event", _envelope(payload, business_id), event_id)


def enqueue_flush(payload: dict, delay_seconds: float) -> None:
    """Agenda el flush de UNA conversación para dentro de `delay_seconds`.

    La espera la hace la cola, no la request del worker: el worker responde
    200 apenas ingesta el mensaje, y esta tarea llega sola cuando toca.

    A propósito SIN nombre determinista (al revés que `enqueue_event`): cada
    mensaje agenda su propio flush. Colapsarlos por nombre parecía más
    prolijo y abre un agujero -- un mensaje que entra justo cuando la tarea
    de su ventana ya corrió se quedaría sin nadie que lo conteste. Que
    sobren flushes es inofensivo y barato: el segundo no encuentra nada
    pendiente (ver la marca de agua `ultimo_mensaje_respondido_at`) y
    termina sin hacer nada.

    Bloqueante: llamar con `run_in_threadpool`.
    """
    queue_path = _queue_path()
    _ensure_queue_in_emulator(queue_path)

    schedule_time = timestamp_pb2.Timestamp()
    schedule_time.FromDatetime(datetime.now(timezone.utc) + timedelta(seconds=delay_seconds))
    get_client().create_task(
        request={
            "parent": queue_path,
            "task": {
                "schedule_time": schedule_time,
                "http_request": _http_request("/tasks/flush-conversation", payload),
            },
        }
    )


def enqueue_incident_timeout(incident_id: str, attempt: int, delay_seconds: float) -> bool:
    """Agenda la revisión de una incidencia de entrega para dentro de
    `delay_seconds` (ver app/services/delivery/incidents.on_timeout). El
    nombre (incidencia + intento) evita agendar dos veces el mismo intento.

    Bloqueante: llamar con `run_in_threadpool`."""
    return _create_named_task(
        None,
        "/tasks/incident-timeout",
        {"incident_id": incident_id, "attempt": attempt},
        f"incident-{incident_id}-{attempt}",
        delay_seconds,
    )


def enqueue_device_heartbeat(business_id: str, heartbeat: dict) -> bool:
    """Heartbeat de una Raspberry, ya autenticado por el webhook, hacia
    /tasks/device-heartbeat del worker. El nombre (negocio + dispositivo +
    hora de la medición) descarta el mismo heartbeat reenviado.

    Bloqueante: llamar con `run_in_threadpool`."""
    return _create_named_task(
        None,
        "/tasks/device-heartbeat",
        {"business_id": business_id, "heartbeat": heartbeat},
        f"heartbeat-{business_id}-{heartbeat['device_id']}-{heartbeat['measured_at']}",
    )
