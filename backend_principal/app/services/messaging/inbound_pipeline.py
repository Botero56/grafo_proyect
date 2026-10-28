"""Procesamiento de un mensaje entrante de WhatsApp, del lado del worker.

Lo invoca `app.services.worker_entry` en dos tiempos, cada uno desde su
propia tarea de Cloud Tasks:

1. `process_incoming_message` INGESTA el evento (Firestore, handoff, menú
   del día, comprobantes) y devuelve el cuerpo de la tarea de flush si quedó
   algo por contestar. No invoca a la IA.
2. `flush_conversation` llega unos segundos después y contesta de una sola
   vez todo lo que el cliente escribió en esa ventana.

La recepción del webhook de Meta/Kapso (firma, split, encolado) NO vive acá:
es `app.services.webhook_entry`, que no carga nada de este módulo.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from app.ai.core.agent_runner import FALLBACK_REPLY, shared_agent_runner
from app.ai.core.audio_processor import AudioProcessingError, AudioProcessor, transcribe
from app.ai.vertical_registry import get_vertical_handlers
from app.firestore_db.firebase_schemas import Businesses, Conversations, Customers, Messages
from app.logging_setup import bind
from app.schemas.enum_schemas import (
    HandoffMotivo,
    IncidentStatus,
    MessageDirection,
    MessageOrigin,
    MessageStatus,
    WebhookEventKind,
    is_business_active,
)
from app.services.conversation.handoff_service import HandoffService
from app.services.firestore_services import firestore_service
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.firestore_webhook_service import WhatsAppWebhookService
from app.config import cloud_tasks_settings
from app.services.daily_menu import menu_image
from app.services.daily_menu.catalog_orders import CatalogOrderHandler
from app.services.delivery import zone_choice
from app.services import privacy
from app.services.daily_menu.orchestrator import MenuIngestionOrchestrator
from app.services.firestore_services.webhook_event_router import (
    WhatsAppWebhookEventRouter,
)
from app.services.messaging.outbound_message_service import OutboundMessageService
from app.services.payments.transfer_receipt import TransferReceiptHandler
from app.services.delivery import incidents, quotes
from app.services.delivery.couriers import HELP_TEXT as COURIER_HELP_TEXT, CourierMessageHandler
from app.services.tasks import cloud_tasks_client
from app.services.time_utils import as_utc
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient

logger = logging.getLogger("whatsapp-webhook")

# Sesiones del agente en memoria, compartidas durante la vida del proceso.
_agent_runner = shared_agent_runner()
_handoff_service = HandoffService()

# motivo_handoff válidos que puede reportar la IA (ver
# request_human_handoff en customer_tools.py). Con cualquier otro valor NO
# hay traspaso: se manda la respuesta de la IA como siempre. Antes un motivo
# desconocido caía a "baja_confianza" y el cliente recibía "te atiende
# alguien" sin haberlo pedido.
_VALID_AI_HANDOFF_MOTIVOS = {
    HandoffMotivo.SOLICITUD_CLIENTE.value,
    HandoffMotivo.PRECIO_VARIABLE.value,
    HandoffMotivo.SIN_DATOS.value,
}

# Lo que la ingesta guarda en Messages.content para una nota de voz (ver
# WhatsAppWebhookService._MEDIA_PLACEHOLDER_TEXT) hasta que se transcribe.
_AUDIO_PLACEHOLDER = WhatsAppWebhookService._MEDIA_PLACEHOLDER_TEXT["audio"]
_UNHEARD_AUDIO = "[nota de voz que no se pudo escuchar]"

# Solo la ve el modelo en ese turno; no se guarda en Messages.
_MENU_SENT_NOTE = (
    "[Nota del sistema, no visible para el cliente: justo antes de este "
    "mensaje se le envió la imagen del menú de hoy (o el link del catálogo). No repitas el menú en "
    "texto salvo que lo pida o no pueda verla: atiende lo que escribió y, si "
    "quiere pedir, pregúntale qué le provoca.]\n"
)


class RetryableTaskError(Exception):
    """Falló ANTES de invocar a la IA y de mandar nada por WhatsApp: el
    worker responde 500 y Cloud Tasks reintenta sin riesgo de duplicarle
    una respuesta al cliente ni de ejecutar dos veces las tools. Cualquier
    otro fallo se sigue tragando con 200 (ver worker_entry)."""


async def process_incoming_message(body: dict, business_id: str | None = None) -> dict | None:
    """Devuelve {business_id, customer_id, conversation_id} si quedó una
    respuesta pendiente, o None si este evento no dejó nada (duplicado,
    status, echo, bot pausado, imagen de menú consumida...). Ese dict es tal
    cual el cuerpo de la tarea de flush (ver cloud_tasks_client.enqueue_flush).

    No se agrupa en memoria ni se espera la ventana acá: el worker agenda la
    tarea de flush a futuro y responde 200 de inmediato.
    """
    try:
        result = await run_in_threadpool(WhatsAppWebhookEventRouter().route, body, business_id)
    except Exception as exc:
        logger.exception("Error procesando el payload del webhook")
        # La ingesta se reintenta: el evento quedó "received" y
        # WhatsAppWebhookService lo reprocesa (ver register_event_once).
        raise RetryableTaskError("Falló la ingesta del evento") from exc

    if not result or result.get("duplicate"):
        return None
    if result.get("error"):
        # Ej. business_not_found: si el número del negocio no coincide, NINGÚN
        # cliente recibe respuesta -- tiene que verse en los logs.
        logger.error("Evento del webhook descartado: %s", result["error"])
        return None

    event_kind = result.get("event_kind")
    if result.get("business") is not None:
        bind(
            business_id=result["business"].bussiness_id,
            customer_id=getattr(result.get("customer"), "customer_id", None),
            conversation_id=getattr(result.get("conversation"), "conversation_id", None),
        )

    # status/history/smb_app_state_sync: ya se persistieron para
    # auditoría/contexto en el router de eventos, no disparan nada más acá.
    if event_kind in (
        WebhookEventKind.STATUS.value,
        WebhookEventKind.HISTORY.value,
        WebhookEventKind.APP_STATE_SYNC.value,
    ):
        return None

    if event_kind == WebhookEventKind.ECHO.value:
        await _process_echo_context(result)
        return None

    if event_kind == WebhookEventKind.MESSAGE.value:
        return await _process_customer_message(result)

    return None


async def _process_echo_context(result: dict) -> None:
    """El echo ya se guardó y el bot ya se pausó (ver
    WhatsAppWebhookEventRouter._process_echo, que es síncrono/Firestore).
    Lo único async que falta es que la IA "vea" lo que el humano dijo, para
    no contradecirlo cuando el bot se reactive."""
    business = result["business"]
    customer = result["customer"]
    conversation = result["conversation"]
    message = result["message"]

    await _agent_runner.append_human_message(
        business_id=business.bussiness_id,
        conversation_id=conversation.conversation_id,
        user_id=customer.customer_id,
        text=message.content,
    )


async def _process_customer_message(result: dict) -> dict | None:
    """Devuelve {business_id, customer_id, conversation_id} si quedó una
    respuesta pendiente; None si cortó antes (bot pausado, imagen de menú
    consumida, error).

    El mensaje ya quedó guardado en Firestore por la ingesta; de contestarlo
    se encarga la tarea de flush que agenda el worker (ver
    flush_conversation)."""
    business = result["business"]
    customer = result["customer"]
    conversation = result["conversation"]
    message = result["message"]

    # Kill switch del negocio: el mensaje queda guardado, pero COMPLETED
    # para que al reactivar no se conteste algo viejo; solo lo nuevo.
    if not is_business_active(business):
        logger.info("Negocio %s apagado (status=%s): mensaje guardado sin IA", business.bussiness_id, business.status)
        message.status = MessageStatus.COMPLETED.value
        await run_in_threadpool(FireStoreCRUDService().update, message)
        return None

    whatsapp_client = WhatsAppClient.for_business(business)

    # Bot pausado (handoff activo en esta conversación): el mensaje del
    # cliente ya se guardó, pero NUNCA pasa por la IA -- solo se avisa al
    # humano que sigue esperando una respuesta.
    #
    # OJO: se compara contra `False` explícito, no `not conversation.bot_activo`.
    # Las conversaciones creadas ANTES de este campo existir no tienen
    # `bot_activo` guardado en Firestore -- fireo lo deserializa como None
    # (no aplica el default=True al leer un campo ausente, solo al
    # serializar una instancia nueva, ver _get_or_create_conversation).
    # `not None` da True, así que con `not conversation.bot_activo` TODA
    # conversación vieja quedaba tratada como pausada y el bot dejaba de
    # responder sin que nadie la hubiera pausado nunca (bug real detectado
    # en producción: 2026-09-21).
    if conversation.bot_activo is False:
        # Reactivación automática por inactividad de ambas partes: si ya
        # pasaron HANDOFF_QUICK_REACTIVATION_SECONDS desde el último
        # mensaje de la encargada, este mismo mensaje del cliente reactiva
        # el bot y sigue el flujo normal más abajo -- nadie tiene que
        # autorizarlo a mano desde el chat admin. Es event-driven (se
        # chequea acá, no por un job programado): 30s es demasiado seguido
        # para Cloud Scheduler, y no hace falta un loop en background
        # porque nada depende de bot_activo hasta que llega un mensaje.
        # Se mide con el momento en que el cliente ESCRIBIÓ (timestamp de
        # WhatsApp), no con cuándo se procesó: Cloud Tasks no garantiza
        # orden, y un mensaje escrito ANTES de que la encargada respondiera
        # (diferencia negativa) nunca debe reactivar el bot.
        sent_at = result.get("wa_sent_at")
        if await run_in_threadpool(_handoff_service.should_auto_reactivate, conversation, sent_at):
            await run_in_threadpool(_handoff_service.reactivate_bot, conversation, sent_at)
        elif await CourierMessageHandler(whatsapp_client).maybe_handle(business, customer, message):
            # Un domiciliario liquidando ("liquidar 4") no espera al humano:
            # la vía rápida no es IA. Lo conversacional sí queda pausado.
            message.status = MessageStatus.COMPLETED.value
            await run_in_threadpool(FireStoreCRUDService().update, message)
            return None
        else:
            # Con el chat en manos de una persona el bot no le contesta: una
            # novedad de entrega esperándolo la resuelve esa persona.
            incident = await run_in_threadpool(
                incidents.find_active_for_customer, business.bussiness_id, customer.customer_id
            )
            if incident is not None and incident.status == IncidentStatus.ESPERANDO_CLIENTE.value:
                await incidents.escalate_busy_customer(business, incident)
            try:
                await _handoff_service.notify_human(
                    business, customer, motivo=conversation.motivo_handoff or "handoff_activo"
                )
            except Exception:
                logger.exception("Error notificando al humano (bot pausado)")
            return None

    try:
        handlers = get_vertical_handlers(business.bussiness_vertical)

        role_info = await run_in_threadpool(
            handlers.resolve_role,
            business_id=business.bussiness_id,
            phone_number=customer.phone_number,
            customer_id=customer.customer_id,
        )
        # Carga del menú del día por imagen: solo aplica a admins ya
        # autorizados (resueltos arriba, la IA nunca decide esto). Si el
        # orchestrator consume el mensaje (imagen, click de botón, o texto
        # mientras hay un borrador pendiente), no se llega a invocar al
        # admin_agent para ESTE mensaje.
        # Del cliente, la foto del comprobante de una transferencia pendiente
        # tampoco pasa por la IA: se valida contra el total del pedido (ver
        # TransferReceiptHandler).
        # Ley 1581: a un cliente sin autorización de datos no se le atiende (ni
        # pasa por la IA) hasta que toque "Acepto"; si no acepta, se borra lo
        # guardado. Admins y domiciliarios son del equipo: no aplica.
        if role_info["role"] == "cliente" and not role_info.get("courier"):
            if not await privacy.gate(business, customer, message):
                return None

        if role_info["role"] == "admin":
            # Cotización con formato fijo ("4000 573001234567"): sin IA.
            handled = await quotes.maybe_handle_admin_quote(
                whatsapp_client, business, customer, message
            ) or await MenuIngestionOrchestrator(whatsapp_client).maybe_handle(
                business, customer, message
            )
        elif role_info["role"] == "domiciliario" or role_info.get("courier"):
            # Domiciliario (también en modo cliente): un comando claro se
            # liquida sin IA. Lo demás sigue al flush, que lo pasa al agente
            # de domiciliarios, o al de clientes si está pidiendo para él.
            # El comprobante de su propio pedido (transferencia) se valida
            # igual que el de cualquier cliente, esté o no en modo cliente.
            handled = await CourierMessageHandler(whatsapp_client).maybe_handle(
                business, customer, message
            ) or await CatalogOrderHandler().maybe_handle(
                business, customer, conversation, message, result.get("order")
            ) or await zone_choice.maybe_handle(
                business, customer, conversation, message
            ) or await TransferReceiptHandler(whatsapp_client).maybe_handle(business, customer, message)
        else:
            # Carrito del catálogo: lo traduce el código. Si quedó completo
            # sigue al agente con el resumen; si espera al admin, se corta acá.
            handled = await CatalogOrderHandler().maybe_handle(
                business, customer, conversation, message, result.get("order")
            ) or await zone_choice.maybe_handle(
                business, customer, conversation, message
            ) or await TransferReceiptHandler(whatsapp_client).maybe_handle(
                business, customer, message
            )
        if handled:
            # Ya quedó atendido: que la próxima tarea de flush no se lo pase
            # también a la IA (ver _claim_pending_messages).
            message.status = MessageStatus.COMPLETED.value
            await run_in_threadpool(FireStoreCRUDService().update, message)
            return None

        # Acá termina el trabajo de este mensaje: ya está en Firestore y la
        # tarea de flush lo va a recoger junto con lo que llegue en los
        # próximos segundos. No se procesa el audio ni se resuelve el agente
        # todavía -- de eso se encarga el flush, con todos los mensajes de la
        # tanda a la vista.
        return {
            "business_id": business.bussiness_id,
            "customer_id": customer.customer_id,
            "conversation_id": conversation.conversation_id,
        }
    except Exception:
        logger.exception("Error preparando la respuesta del agente")
        return None


# Techos del claim. Sin ellos, una conversación SIN marca de agua (todas las
# que existen desde antes de este campo) haría que el primer flush le pasara
# a la IA el historial entero de la charla como si el cliente lo acabara de
# escribir.
#
# Por antigüedad: un mensaje de hace rato ya no es algo que el cliente esté
# esperando -- contestarlo ahora confunde más que ayudar.
_CLAIM_MAX_AGE = timedelta(minutes=10)
# Por cantidad: aunque entren en la ventana, se contestan los últimos N. Es
# un tope de seguridad contra una ráfaga anómala, no algo que se espere
# alcanzar (la ventana normal son segundos).
_CLAIM_MAX_MESSAGES = 20
# Tope de la ventana deslizante: desde el primer mensaje sin contestar, nunca
# se espera más que esto aunque el cliente siga escribiendo.
_MAX_BATCH_WAIT_SECONDS = 20


def _is_from_customer(message) -> bool:
    """Un mensaje entrante del cliente se reconoce por lo que NO es.

    OJO: la ingesta (WhatsAppWebhookService.process_incoming_payload) NO
    escribe `direction` ni `origin` en los mensajes del cliente -- quedan en
    None. Solo los llevan las respuestas de la IA (saliente/ia) y los echoes
    de un humano (saliente/humano). Filtrar por `direction == "entrante"`
    parece lo natural y no matchea NADA: el flush encontraba cero pendientes
    y terminaba en silencio (medido en producción, 2026-09-24).
    """
    if getattr(message, "direction", None) == MessageDirection.SALIENTE.value:
        return False
    return getattr(message, "origin", None) not in (
        MessageOrigin.IA.value,
        MessageOrigin.HUMANO.value,
        # Chats importados al conectar el número: ya pasaron, nadie los espera.
        MessageOrigin.HISTORIAL.value,
    )


def _claim_pending_messages(conversation) -> list:
    """Se lleva los mensajes del cliente que todavía nadie contestó y avanza
    la marca de agua, en ese orden.

    Avanzar ANTES de invocar a la IA es deliberado: si dos tareas de flush
    coinciden, la segunda encuentra la lista vacía y no hace nada. La marca
    se avanza con compare-and-set (transacción sobre la conversación): si
    otra tarea la movió entre la lectura y la escritura, esta relee y
    recalcula en vez de reclamar lo mismo. Al revés
    (contestar y después marcar) las dos contestarían y el cliente recibiría
    la respuesta duplicada, que es lo único que este diseño no se permite.
    El precio es que si la IA falla después del claim, esos mensajes quedan
    sin respuesta -- se registra en el log y el cliente puede volver a
    escribir.

    Se filtra en Python en vez de con un `where created_at >`: eso exigiría
    un índice compuesto en Firestore (conversation_id + created_at) que hoy
    no existe, y una conversación tiene pocos mensajes.
    """
    for _ in range(3):
        pending = _unanswered_messages(conversation)
        if not pending:
            return []

        if len(pending) > _CLAIM_MAX_MESSAGES:
            logger.warning(
                "Conversación %s con %d mensajes pendientes: se contestan los últimos %d",
                conversation.conversation_id,
                len(pending),
                _CLAIM_MAX_MESSAGES,
            )
            pending = pending[-_CLAIM_MAX_MESSAGES:]

        # La marca avanza al último de la tanda: lo que quedó fuera por los
        # techos no se vuelve a mirar, que es la intención.
        previous = getattr(conversation, "ultimo_mensaje_respondido_at", None)
        mark = as_utc(pending[-1].created_at)
        if firestore_service.compare_and_set(conversation, "ultimo_mensaje_respondido_at", previous, mark):
            conversation.ultimo_mensaje_respondido_at = mark
            return pending
        # Otra tarea de flush avanzó la marca primero: se relee y se recalcula.
        fresh = Conversations.collection.get(conversation.conversation_id)
        conversation.ultimo_mensaje_respondido_at = fresh.ultimo_mensaje_respondido_at if fresh else None
    logger.warning("No se pudo reclamar la conversación %s: demasiada contención", conversation.conversation_id)
    return []


def _unanswered_messages(conversation) -> list:
    """Mensajes del cliente posteriores a la marca de agua, en orden."""
    watermark = getattr(conversation, "ultimo_mensaje_respondido_at", None)
    cutoff = datetime.now(timezone.utc) - _CLAIM_MAX_AGE
    if watermark is not None:
        cutoff = max(cutoff, as_utc(watermark))

    entrantes = [
        m
        for m in Messages.collection.filter(
            conversation_id=conversation.conversation_id
        ).fetch()
        if _is_from_customer(m)
        and getattr(m, "created_at", None) is not None
        # Atendido fuera de la IA (flujo del menú del día). Sin esto, la foto,
        # los botones y las correcciones del admin le llegaban a la IA
        # pegados al siguiente mensaje (prueba real 2026-09-30).
        and getattr(m, "status", None) != MessageStatus.COMPLETED.value
    ]
    entrantes.sort(key=lambda m: as_utc(m.created_at))
    return [m for m in entrantes if as_utc(m.created_at) > cutoff]


def _seconds_until_quiet(pending: list) -> float:
    """Cuánto falta para que el cliente lleve MESSAGE_FLUSH_DELAY_SECONDS
    sin escribir. 0 = contestar ya: está callado, o la tanda ya esperó
    _MAX_BATCH_WAIT y no se le hace esperar más aunque siga escribiendo."""
    if not pending:
        return 0
    now = datetime.now(timezone.utc)
    if (now - as_utc(pending[0].created_at)).total_seconds() >= _MAX_BATCH_WAIT_SECONDS:
        return 0
    quiet = cloud_tasks_settings.MESSAGE_FLUSH_DELAY_SECONDS
    return max(0.0, quiet - (now - as_utc(pending[-1].created_at)).total_seconds())


async def flush_conversation(business_id: str, customer_id: str, conversation_id: str) -> bool:
    """Contesta de una sola vez todo lo que el cliente escribió en la
    ventana. Lo invoca la tarea diferida del worker (ver
    worker_entry.flush_conversation_task).

    Devuelve False si no había nada pendiente (otra tarea de flush ya se lo
    llevó), True si se generó una respuesta.
    """
    crud = FireStoreCRUDService()
    business = await run_in_threadpool(crud.get, Businesses, business_id)
    customer = await run_in_threadpool(crud.get, Customers, customer_id)
    conversation = await run_in_threadpool(crud.get, Conversations, conversation_id)
    if not business or not customer or not conversation:
        logger.warning(
            "Flush sin contexto (business=%s customer=%s conversation=%s)",
            business_id,
            customer_id,
            conversation_id,
        )
        return False

    # Ventana deslizante: si el cliente sigue escribiendo, se espera a que
    # pare (con tope) en vez de contestar la primera mitad de la ráfaga y
    # dejar la otra para una segunda respuesta que se cruza con la primera.
    wait = _seconds_until_quiet(await run_in_threadpool(_unanswered_messages, conversation))
    if wait > 0:
        try:
            await run_in_threadpool(
                cloud_tasks_client.enqueue_flush,
                {"business_id": business_id, "customer_id": customer_id, "conversation_id": conversation_id},
                wait,
            )
            return False
        except Exception:
            logger.exception("No se pudo posponer el flush; se contesta ya")

    pending = await run_in_threadpool(_claim_pending_messages, conversation)
    if not pending:
        return False

    # Se apagó el negocio mientras la tanda esperaba: quedan reclamados y sin respuesta.
    if not is_business_active(business):
        logger.info("Flush omitido: negocio %s apagado (%d mensaje(s))", business_id, len(pending))
        return False

    # La encargada respondió desde el celular (echo) mientras esta tanda
    # esperaba en la cola: la conversación es suya. Los mensajes ya se
    # reclamaron (la marca de agua avanzó), así que no se le pasan a la IA
    # ni ahora ni cuando el bot se reactive.
    if conversation.bot_activo is False:
        logger.info(
            "Flush omitido: el bot se pausó (%s) antes de contestar %d mensaje(s)",
            conversation.motivo_handoff,
            len(pending),
        )
        return False

    try:
        whatsapp_client = WhatsAppClient.for_business(business)

        handlers = get_vertical_handlers(business.bussiness_vertical)
        role_info = await run_in_threadpool(
            handlers.resolve_role,
            business_id=business.bussiness_id,
            phone_number=customer.phone_number,
            customer_id=customer.customer_id,
        )
        agent = handlers.get_agent(role_info["role"])
    except Exception as exc:
        # La marca de agua ya avanzó pero todavía no se invocó a la IA ni se
        # mandó nada: se devuelve a justo antes de la tanda para que el
        # reintento de Cloud Tasks vuelva a tomar estos mensajes en vez de
        # dejarlos sin respuesta.
        before = as_utc(pending[0].created_at) - timedelta(microseconds=1)
        # Solo si nadie la movió desde el claim (si no, ya otro la tomó).
        if await run_in_threadpool(
            firestore_service.compare_and_set,
            conversation,
            "ultimo_mensaje_respondido_at",
            conversation.ultimo_mensaje_respondido_at,
            before,
        ):
            conversation.ultimo_mensaje_respondido_at = before
        raise RetryableTaskError("Falló la preparación del turno") from exc

    # Doble check azul + "escribiendo..." mientras se arma la respuesta.
    last_wamid = getattr(pending[-1], "whatsapp_message_id", None)
    if last_wamid:
        try:
            await whatsapp_client.mark_read(last_wamid)
        except Exception:
            logger.exception("No se pudo marcar como leído")

    # Regla de la imagen del menú: la primera vez del día (o si el admin
    # publicó otra) sale sola, ANTES de la respuesta, sin depender de que el
    # modelo detecte la intención de pedir. Best-effort: si falla, la IA
    # contesta igual y puede mostrar el menú en texto.
    menu_note = ""
    if role_info["role"] == "cliente":
        try:
            if await menu_image.send_menu_image(whatsapp_client, business, customer, conversation, auto=True):
                menu_note = _MENU_SENT_NOTE
        except Exception:
            logger.exception("No se pudo enviar la imagen del menú")
        # Novedad de entrega abierta: su IA debe leer lo que escribe como
        # respuesta a eso (ver delivery/incidents.py).
        try:
            incident = await run_in_threadpool(
                incidents.find_active_for_customer, business.bussiness_id, customer.customer_id
            )
            if incident is not None:
                menu_note += incidents.customer_note(incident)
        except Exception:
            logger.exception("No se pudo revisar la incidencia de entrega del cliente")

    # Cada nota de voz de la tanda se transcribe ANTES de que la IA razone y
    # el texto reemplaza el placeholder en Messages: queda en el historial y
    # en el resumen, y "no, espera, quita eso" por audio pesa igual que por
    # texto. Antes el audio viajaba crudo solo en ese turno (y solo el último
    # de la tanda), y en la sesión quedaba "[audio del cliente, ya respondido]".
    for message in pending:
        if getattr(message, "type", None) == "audio" and message.content == _AUDIO_PLACEHOLDER:
            message.content = await _transcribe(whatsapp_client, message)
            await run_in_threadpool(FireStoreCRUDService().update, message)

    logger.info(
        "Tanda agrupada",
        extra={
            "fields": {
                "mensajes": len(pending),
                "audios": sum(1 for m in pending if getattr(m, "type", None) == "audio"),
                "menu_imagen": bool(menu_note),
                "wamids": [getattr(m, "whatsapp_message_id", None) for m in pending],
            }
        },
    )
    # Las fotos de un admin las lee el sistema (menú/carta, ver
    # MenuIngestionOrchestrator), que tarda más que esta tanda: si la IA las
    # veía como "[Imagen recibida]" contestaba "el sistema está procesando…".
    customer_text = "\n".join(
        f"(nota de voz) {m.content}" if getattr(m, "type", None) == "audio" else m.content
        for m in pending
        if m.content and not (role_info["role"] == "admin" and getattr(m, "type", None) == "image")
    )

    await _generate_and_send_reply(
        whatsapp_client=whatsapp_client,
        business=business,
        customer=customer,
        conversation=conversation,
        agent=agent,
        message_text=menu_note + customer_text if customer_text.strip() else "",
        role=role_info["role"],
    )
    return True


async def _transcribe(whatsapp_client: WhatsAppClient, message) -> str:
    """Transcripción de la nota de voz, o un aviso para que la IA pida
    repetirla (nunca se adivina lo que dijo)."""
    try:
        audio = await AudioProcessor(whatsapp_client).process(message.media_id, message.mime_type)
        return await transcribe(audio)
    except AudioProcessingError as e:
        logger.warning("Audio sin transcribir (media_id=%s): %s", message.media_id, e)
    except Exception:
        logger.exception("Audio sin transcribir por un error inesperado (media_id=%s)", message.media_id)
    return _UNHEARD_AUDIO


async def _generate_and_send_reply(
    *,
    whatsapp_client: WhatsAppClient,
    business,
    customer,
    conversation,
    agent,
    message_text: str,
    role: str = "cliente",
) -> None:
    """Invoca al agente con el texto ya agrupado de la tanda y manda la
    respuesta (ver flush_conversation)."""
    # Nada que leer: tipos que la ingesta guarda sin contenido (WhatsApp
    # manda "unsupported" para varias fotos juntas o tipos que la API no
    # soporta, y también ubicación, encuestas...). Sin esto la IA contestaba
    # un mensaje vacío con algo fuera de contexto (prueba real 2026-10-05).
    if not message_text.strip():
        logger.info("Turno sin texto: no se invoca a la IA")
        return

    agent_reply = await _agent_runner.get_reply(
        agent=agent,
        business_id=business.bussiness_id,
        conversation_id=conversation.conversation_id,
        user_id=customer.customer_id,
        message_text=message_text,
        # El domiciliario que hoy pide como cliente no mezcla historiales.
        session_scope="domiciliario" if role == "domiciliario" else None,
    )
    if role == "domiciliario" and agent_reply.text == FALLBACK_REPLY:
        agent_reply.text = COURIER_HELP_TEXT  # la vía rápida sigue sirviendo sin el modelo

    motivo = agent_reply.handoff_reason
    if agent_reply.requires_human and motivo not in _VALID_AI_HANDOFF_MOTIVOS:
        logger.warning("Handoff ignorado: motivo no válido %r; se manda la respuesta de la IA", motivo)
    elif agent_reply.requires_human:
        # OJO: esto NO pausa el bot (bot_activo sigue True). Antes llamaba a
        # _handoff_service.pause_bot acá mismo, apenas la IA detectaba que
        # el cliente pedía un humano -- pero nada garantiza que un humano
        # responda pronto, así que el cliente se quedaba sin respuesta de
        # NADIE hasta que alguien reactivara el bot a mano (bug reportado en
        # producción, 2026-09-22). Ahora el bot sigue atendiendo normalmente
        # al cliente; el único evento que de verdad pausa la conversación es
        # que la encargada responda desde la app de WhatsApp Business (echo
        # real, ver _process_echo_context / webhook_event_router.py._process_echo).
        transfer_message = await run_in_threadpool(
            _handoff_service.get_transfer_message, business.bussiness_id
        )
        await OutboundMessageService(whatsapp_client).send_and_persist(
            business_id=business.bussiness_id,
            customer_id=customer.customer_id,
            conversation_id=conversation.conversation_id,
            to_phone=customer.phone_number,
            text=transfer_message,
            origin=MessageOrigin.IA.value,
        )
        try:
            await _handoff_service.notify_handoff(
                business,
                customer,
                resumen=agent_reply.handoff_summary,
                tema=agent_reply.handoff_topic,
            )
        except Exception:
            logger.exception("Error notificando al humano (handoff de la IA)")
        return

    await OutboundMessageService(whatsapp_client).send_and_persist(
        business_id=business.bussiness_id,
        customer_id=customer.customer_id,
        conversation_id=conversation.conversation_id,
        to_phone=customer.phone_number,
        text=agent_reply.text,
        origin=MessageOrigin.IA.value,
    )

    # El admin_agent (generate_excel_report) puede haber dejado un archivo
    # listo para enviar. Se manda como mensaje aparte, después del texto,
    # para que el admin primero lea el resumen y luego reciba el adjunto.
    if agent_reply.pending_document:
        await _send_pending_document(
            whatsapp_client, customer.phone_number, agent_reply.pending_document
        )


async def _send_pending_document(
    whatsapp_client: WhatsAppClient, to_phone: str, pending_document: dict
) -> None:
    file_path = Path(pending_document["path"])
    filename = pending_document.get("filename", file_path.name)
    try:
        file_bytes = file_path.read_bytes()
    except OSError:
        logger.exception("No se pudo leer el reporte generado: %s", file_path)
        return
    # El archivo es temporal: una vez en memoria ya no hace falta en disco
    # (en Cloud Run el disco es RAM, y los reportes se acumulaban).
    file_path.unlink(missing_ok=True)

    media_id = await whatsapp_client.upload_media(
        file_bytes,
        filename,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    if not media_id:
        logger.error("No se pudo subir el reporte a WhatsApp: %s", file_path)
        return

    sent = await whatsapp_client.send_document(
        to_phone, media_id=media_id, filename=filename
    )
    if not sent:
        logger.error("No se pudo enviar el reporte por WhatsApp: %s", file_path)
