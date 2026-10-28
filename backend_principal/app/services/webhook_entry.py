"""Webhook liviano: verifica la firma, separa los eventos y encola cada uno
en Cloud Tasks para que los procese `worker_entry.py`.

Deliberadamente NO importa google-adk, fireo ni Firestore: el negocio vive
en el worker (`app.services.messaging.inbound_pipeline`). Eso mantiene el
arranque en frío de este servicio al mínimo y hace que un reinicio del
worker no pierda mensajes, porque Cloud Tasks los retiene en la cola. Su
imagen (Dockerfile.webhook) tampoco instala el grupo de dependencias
`worker` (google-adk, pandas, matplotlib...).

Es el único servicio público: recibe el webhook de Meta/Kapso y sirve la
landing.

    uvicorn app.services.webhook_entry:app --port 8080

En local: `docker compose up` (ver docker-compose.yml) lo levanta junto al
worker y al emulador de Cloud Tasks.
"""

import hmac
import json
import logging
import re
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.config import whatsapp_settings
from app.logging_setup import bind, configure_logging
from app.schemas.api.device_schemas import BUSINESS_ID_PATTERN, HeartbeatPayload
from app.services import secrets, tenants
from app.services.messaging.webhook_payload import (
    SYNC_FIELDS,
    content_event_id,
    event_field,
    event_id,
    event_timestamp,
    phone_number_ids,
    split_events,
    split_history,
)
from app.services.landing import router as landing_router
from app.services import privacy_page
from app.services.tasks import cloud_tasks_client
from app.services.whatsapp_client.signature import (
    KAPSO_SIGNATURE_HEADER,
    verify_kapso_signature,
    verify_webhook_signature,
)

logger = logging.getLogger("whatsapp-webhook-entry")

configure_logging()

app = FastAPI(title="j2ys-webhook", docs_url=None, redoc_url=None)

# Landing pública "próximamente" en GET /.
app.include_router(landing_router)
# Política de datos pública de cada negocio (el enlace que reciben los clientes).
app.include_router(privacy_page.router)


def _log_failed_statuses(payload: dict) -> None:
    try:
        statuses = [
            status
            for entry in payload.get("entry") or []
            for change in entry.get("changes") or []
            for status in (change.get("value") or {}).get("statuses") or []
        ]
        for status in statuses:
            if status.get("status") != "failed":
                continue
            error = (status.get("errors") or [{}])[0]
            logger.error(
                "WhatsApp no entregó el mensaje %s: code=%s %s",
                status.get("id"),
                error.get("code"),
                error.get("title") or error.get("message"),
            )
    except Exception:
        logger.exception("No se pudo leer un status del webhook")


@app.get("/webhook")
async def verify_webhook(request: Request) -> Response:
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and whatsapp_settings.VERIFY_TOKEN and token == whatsapp_settings.VERIFY_TOKEN:
        logger.info("✅ Webhook verificado correctamente")
        return Response(content=challenge, media_type="text/plain")

    logger.warning("❌ Falló la verificación del webhook")
    return Response(status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request) -> Response:
    raw_body = await request.body()

    # La firma se calcula sobre el body CRUDO. Acepta Meta directo
    # (X-Hub-Signature-256 con APP_SECRET) o Kapso (X-Webhook-Signature con
    # KAPSO_WEBHOOK_SECRET); sin el secreto de una vía, esa vía se rechaza
    # siempre.
    if not verify_webhook_signature(
        raw_body,
        request.headers,
        app_secret=whatsapp_settings.APP_SECRET,
        kapso_secret=whatsapp_settings.KAPSO_WEBHOOK_SECRET,
    ):
        logger.warning("❌ Firma de webhook inválida (Meta o Kapso), rechazando")
        return Response(status_code=401)

    try:
        body = json.loads(raw_body)
    except ValueError:
        return Response(status_code=400)
    if not isinstance(body, dict):
        return Response(status_code=400)

    return Response(status_code=502 if await _enqueue_events(body) else 200)


async def _enqueue_events(body: dict, business_id: Optional[str] = None, max_age_seconds: Optional[int] = None) -> bool:
    """Separa y encola cada evento. Devuelve True si alguno no se pudo encolar."""
    # Si encolar falla (Cloud Tasks caído) se responde error para que Meta
    # reintente la entrega completa más tarde. Es seguro: los eventos que SÍ
    # se encolaron no se duplican en el reintento, porque el nombre de la
    # tarea es determinista (ver cloud_tasks_client.enqueue_event).
    any_failed = False
    for payload in split_events(body):
        # Coexistencia: historial y contactos llegan en ráfaga al conectar
        # un número y no traen wamid. Van a su propia cola (más lenta) con
        # un nombre de tarea derivado del contenido, para no atrasar los
        # mensajes nuevos ni encolar dos veces una reentrega de Meta.
        if event_field(payload) in SYNC_FIELDS:
            for piece in split_history(payload):
                piece_id = content_event_id(piece)
                try:
                    await run_in_threadpool(cloud_tasks_client.enqueue_sync_event, piece, piece_id, business_id)
                except Exception:
                    any_failed = True
                    logger.exception("No se pudo encolar el evento de sincronización %s", piece_id[:40])
            continue

        wamid = event_id(payload)
        if wamid is None:
            # statuses: no traen wamid propio, así que no hay con qué
            # deduplicar la tarea, y nada del flujo los necesita: se
            # descartan acá en vez de encolarlos sin protección contra
            # duplicados. (history/smb_app_state_sync se encolan arriba.)
            # Lo único que se rescata es un status "failed": es la forma en
            # que Meta avisa que una respuesta que aceptó NO se entregó.
            _log_failed_statuses(payload)
            continue

        bind(wamid=wamid)
        sent_at = event_timestamp(payload)
        if max_age_seconds and sent_at and time.time() - sent_at > max_age_seconds:
            # 200 y sin tarea: un error haría que Kapso reintente (y pause el
            # webhook si se repite), y el mensaje ya no se va a contestar.
            logger.warning(
                "Mensaje %s descartado por viejo (%d s)", wamid, int(time.time() - sent_at),
                extra={"fields": {"security_event": "stale_event"}},
            )
            continue
        try:
            await run_in_threadpool(cloud_tasks_client.enqueue_event, payload, wamid, business_id)
        except Exception:
            any_failed = True
            # Nunca se loguea el payload: lleva teléfonos y contenido del cliente.
            logger.exception("No se pudo encolar el evento %s", wamid)

    return any_failed


def _security_event(request: Request, event: str, message: str, **fields) -> None:
    # Detrás del frontend de Cloud Run, la IP real es la primera de X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for", "")
    client_ip = forwarded.split(",")[0].strip() or (request.client.host if request.client else None)
    logger.warning(message, extra={"fields": {"security_event": event, "ip": client_ip, **fields}})


@app.post("/webhook/kapso/{tenant_key}")
async def receive_tenant_webhook(tenant_key: str, request: Request) -> Response:
    """Webhook de Kapso de UN negocio: se verifica con el secreto de ese
    negocio y cada evento se encola ya con su business_id. Orden y códigos
    en PLAN_SEGURIDAD_DOMICILIOS.md §3 / SECURITY.md."""
    raw_body = await request.body()

    try:
        tenant = await run_in_threadpool(tenants.resolve_tenant, tenant_key)
    except Exception:
        logger.exception("No se pudo resolver el tenant del webhook")
        return Response(status_code=503)
    if tenant is None:
        # Solo el prefijo: la clave completa es lo que habilita la URL.
        _security_event(request, "tenant_key_unknown", "Webhook con tenantKey desconocida", tenant_key_prefix=tenant_key[:8])
        return Response(status_code=404)
    bind(business_id=tenant.business_id)

    signing_secrets = []
    if tenant.webhook_secret_ref:
        try:
            signing_secrets = await run_in_threadpool(secrets.get_secret_versions, tenant.webhook_secret_ref)
        except Exception:
            logger.exception("No se pudo leer el secreto del webhook del negocio")
    if not signing_secrets or not tenant.phone_number_id:
        # Configuración incompleta (o Secret Manager caído): 503 para que
        # Kapso reintente en vez de perder el evento.
        logger.error("Negocio sin secreto de webhook o sin phone_number_id: no se puede verificar")
        return Response(status_code=503)

    signature = request.headers.get(KAPSO_SIGNATURE_HEADER)
    # latest y la versión anterior: rotar el secreto no corta la entrega.
    if not any(verify_kapso_signature(raw_body, signature, secret) for secret in signing_secrets):
        _security_event(request, "bad_signature", "Firma de webhook inválida para el tenant")
        return Response(status_code=401)

    try:
        body = json.loads(raw_body)
    except ValueError:
        return Response(status_code=400)
    if not isinstance(body, dict):
        return Response(status_code=400)

    foreign = phone_number_ids(body) - {tenant.phone_number_id}
    if foreign:
        _security_event(
            request, "phone_number_id_mismatch", "El webhook trae un número que no es del tenant",
            phone_number_ids=sorted(foreign),
        )
        return Response(status_code=403)

    logger.info("Webhook de Kapso", extra={"fields": {"idempotency_key": request.headers.get("X-Idempotency-Key")}})
    failed = await _enqueue_events(
        body, business_id=tenant.business_id, max_age_seconds=whatsapp_settings.WEBHOOK_MAX_EVENT_AGE_SECONDS
    )
    return Response(status_code=502 if failed else 200)


# --- Heartbeat de las Raspberries -------------------------------------------
# Mismo contrato que /print (RUNBOOK §4): Bearer <token del negocio> y
# X-Business-Id. Se verifica acá y se encola; el worker lo guarda.
_MAX_HEARTBEAT_BYTES = 8 * 1024


def _printer_token_matches(business_id: str, sent: str) -> bool:
    expected = secrets.get_secret_or_none(secrets.secret_ref(secrets.PRINTER_TOKEN, business_id))
    return bool(expected) and hmac.compare_digest(sent.encode(), expected.encode())


@app.post("/devices/heartbeat", status_code=202)
async def device_heartbeat(
    request: Request,
    authorization: str = Header(default=""),
    x_business_id: str = Header(default=""),
) -> dict:
    scheme, _, token = authorization.partition(" ")
    # shortcut: sin rate limit propio; un id con formato válido pero falso cuesta
    # una lectura de Secret Manager. Poner Cloudflare/Armor delante si hay abuso.
    if scheme != "Bearer" or not token or not re.fullmatch(BUSINESS_ID_PATTERN, x_business_id):
        raise HTTPException(status_code=401, detail="token inválido")
    if not await run_in_threadpool(_printer_token_matches, x_business_id, token.strip()):
        raise HTTPException(status_code=401, detail="token inválido")
    raw = await request.body()
    if len(raw) > _MAX_HEARTBEAT_BYTES:
        raise HTTPException(status_code=413, detail="heartbeat demasiado grande")
    try:
        heartbeat = HeartbeatPayload.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_input=False, include_url=False))
    await run_in_threadpool(
        cloud_tasks_client.enqueue_device_heartbeat, x_business_id, heartbeat.model_dump(mode="json")
    )
    return {"accepted": True}
