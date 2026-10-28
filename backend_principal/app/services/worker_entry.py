"""Worker privado: procesa lo que encola `webhook_entry.py` vía Cloud Tasks
y sirve la API admin.

Dos endpoints de tareas, y la diferencia entre ellos es el punto del diseño:

- `/tasks/process-event` INGESTA un mensaje y termina en ~1 s. No contesta:
  agenda una tarea de flush a futuro y responde 200.
- `/tasks/flush-conversation` llega unos segundos después y CONTESTA, de una
  sola vez, todo lo que el cliente haya escrito en esa ventana.

Los webhooks de coexistencia `history`/`smb_app_state_sync` entran por
`/tasks/process-event`, desde la cola de historial.

La espera del agrupado la hace la cola (`schedule_time`), no una request
abierta. Antes el worker retenía la petición durante toda la ventana, y eso
costaba CPU facturada y llegó a pasarse del timeout de Cloud Run: dos 504 en
producción el 2026-09-23, que Cloud Tasks reintentó.

La lógica de negocio vive en `app.services.messaging.inbound_pipeline`.

Además monta la API admin (`/api/v1/admin/*`, ver app/api/v1/router.py),
incluidos los jobs que dispara Cloud Scheduler (`/api/v1/admin/jobs/*`).

Este servicio NO debe exponerse públicamente en producción: solo Cloud Tasks
y Cloud Scheduler deben poder invocarlo, con un token OIDC que Cloud Run
valida por IAM al desplegarlo como servicio privado
(`--no-allow-unauthenticated`). Para usar la API admin a mano:
`gcloud run services proxy <servicio> --region us-central1`. El chequeo de
la cabecera `X-CloudTasks-QueueName` es defensa en profundidad -- sirve
para notar que algo llegó sin pasar por la cola --, NO el control de acceso.

Límites conocidos:

1. Solo se responde 500 (Cloud Tasks reintenta) ante `RetryableTaskError`:
   fallos ANTES de invocar a la IA y de mandar nada (ingesta, preparación
   del turno). Todo lo demás responde 200, porque todavía no hay
   idempotencia de SALIDA y reintentar un turno que ya mandó la respuesta
   por WhatsApp se la duplicaría al cliente. Los reintentos tienen tope
   (_MAX_RETRIES) para que un evento envenenado no se reintente por días.
2. Los locks por conversación de `AgentRunnerService` viven en memoria del
   proceso. Con más de una instancia, dos flush de la misma conversación
   pueden correr a la vez; hasta que exista una lease distribuida,
   desplegar con `--max-instances 1`.

    uvicorn app.services.worker_entry:app --port 8081
"""

import asyncio
import logging

from fastapi import Depends, FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

from app.api.v1.router import api_router
from app.config import cloud_tasks_settings
from app.logging_setup import bind, configure_logging
from app.services.delivery import incidents
from app.services.messaging import inbound_pipeline
from app.services.messaging.webhook_payload import event_id
from app.services.tasks import cloud_tasks_client
from app.services.tasks.task_auth import require_task_oidc
from app.services.devices import devices
from app.schemas.api.device_schemas import HeartbeatPayload
from pydantic import ValidationError

logger = logging.getLogger("whatsapp-worker")

# La cola permite 100 intentos (hasta horas de backoff); un fallo que no se
# arregla en 5 no se va a arreglar solo y el cliente ya no espera respuesta.
_MAX_RETRIES = 5

configure_logging()

app = FastAPI(title="j2ys-worker", docs_url=None, redoc_url=None)
app.include_router(api_router, prefix="/api/v1")


def _warn_if_not_from_queue(request: Request, path: str) -> None:
    if "x-cloudtasks-queuename" not in request.headers:
        logger.warning(
            "POST a %s sin cabecera X-CloudTasks-QueueName (normal con el "
            "emulador local; en Cloud Run el control de acceso real es "
            "IAM/OIDC, no esta cabecera).",
            path,
        )


def _retry_response(request: Request, what: str) -> Response:
    """500 para que Cloud Tasks reintente, o 200 si ya se agotó el tope."""
    attempt = int(request.headers.get("x-cloudtasks-taskretrycount", "0")) + 1
    if attempt > _MAX_RETRIES:
        logger.exception("%s: intento %d, se abandona sin reintentar", what, attempt)
        return Response(status_code=200)
    logger.exception("%s: intento %d, se responde 500 para que Cloud Tasks reintente", what, attempt)
    return Response(status_code=500)


@app.post("/tasks/process-event", dependencies=[Depends(require_task_oidc)])
async def process_event_task(request: Request) -> Response:
    _warn_if_not_from_queue(request, "/tasks/process-event")

    body = await request.json()
    # Sobre {business_id, event}; las tareas encoladas antes del cambio
    # traen el payload de Meta solo (nunca tiene la clave "event").
    event, business_id = (body["event"], body.get("business_id")) if "event" in body else (body, None)
    bind(wamid=event_id(event), business_id=business_id)
    try:
        pending = await inbound_pipeline.process_incoming_message(event, business_id)
    except inbound_pipeline.RetryableTaskError:
        return _retry_response(request, "Falló la ingesta")
    except Exception:
        logger.exception("Error ingestando la tarea; se responde 200 para no reintentar")
        return Response(status_code=200)

    # La ingesta ya guardó el mensaje; contestarlo es trabajo de la tarea de
    # flush. None = no quedó nada pendiente (duplicado, status, echo, bot
    # pausado, imagen de menú consumida...).
    if pending is None:
        return Response(status_code=200)

    delay = cloud_tasks_settings.MESSAGE_FLUSH_DELAY_SECONDS
    try:
        await run_in_threadpool(cloud_tasks_client.enqueue_flush, pending, delay)
    except Exception:
        # Sin la tarea de flush nadie contestaría este mensaje (la ingesta
        # ya lo marcó como procesado, así que un reintento lo vería como
        # duplicado). Se contesta desde esta misma request, esperando la
        # ventana de agrupado como lo haría la cola.
        logger.exception("No se pudo agendar el flush; se contesta desde esta misma tarea")
        await asyncio.sleep(delay)
        try:
            await inbound_pipeline.flush_conversation(**pending)
        except Exception:
            logger.exception("Error contestando la conversación tras fallar el agendado del flush")

    return Response(status_code=200)


@app.post("/tasks/incident-timeout", dependencies=[Depends(require_task_oidc)])
async def incident_timeout_task(request: Request) -> Response:
    """Temporizador de una incidencia de entrega: reintenta con el cliente o
    escala al admin (ver delivery/incidents.on_timeout). Idempotente: si se
    reintenta, el intento ya tomado no se repite."""
    _warn_if_not_from_queue(request, "/tasks/incident-timeout")
    body = await request.json()
    bind(business_id=body.get("business_id"))
    try:
        outcome = await incidents.on_timeout(body["incident_id"], int(body["attempt"]))
    except Exception:
        return _retry_response(request, "Falló el temporizador de la incidencia")
    logger.info("Temporizador de incidencia %s: %s", body["incident_id"], outcome)
    return Response(status_code=200)


@app.post("/tasks/device-heartbeat", dependencies=[Depends(require_task_oidc)])
async def device_heartbeat_task(request: Request) -> Response:
    """Guarda el último estado de una Raspberry (el webhook ya verificó su token)."""
    _warn_if_not_from_queue(request, "/tasks/device-heartbeat")
    body = await request.json()
    bind(business_id=body.get("business_id"))
    try:
        heartbeat = HeartbeatPayload.model_validate(body["heartbeat"])
        await run_in_threadpool(devices.record_heartbeat, body["business_id"], heartbeat)
    except ValidationError:
        logger.exception("Heartbeat inválido; se descarta")
        return Response(status_code=200)
    except Exception:
        return _retry_response(request, "No se pudo guardar el heartbeat")
    return Response(status_code=200)


@app.post("/tasks/flush-conversation", dependencies=[Depends(require_task_oidc)])
async def flush_conversation_task(request: Request) -> Response:
    _warn_if_not_from_queue(request, "/tasks/flush-conversation")

    body = await request.json()
    bind(
        business_id=body.get("business_id"),
        customer_id=body.get("customer_id"),
        conversation_id=body.get("conversation_id"),
    )
    try:
        await inbound_pipeline.flush_conversation(
            business_id=body["business_id"],
            customer_id=body["customer_id"],
            conversation_id=body["conversation_id"],
        )
    except inbound_pipeline.RetryableTaskError:
        return _retry_response(request, "Falló la preparación del turno")
    except Exception:
        logger.exception("Error contestando la conversación; se responde 200 para no reintentar")

    return Response(status_code=200)
