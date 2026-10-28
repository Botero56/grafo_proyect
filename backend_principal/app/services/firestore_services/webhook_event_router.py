"""
Enrutador de eventos del webhook de WhatsApp Cloud API en modo Coexistencia.

Un mismo endpoint (`POST /webhook` de app/services/webhook_entry.py) recibe
TODO lo que manda Meta, y el worker lo procesa acá: mensajes de cliente, status updates (sent/delivered/read/failed),
ecos de mensajes que un humano mandó desde la app de WhatsApp Business
(`smb_message_echoes`), y opcionalmente `history`/`smb_app_state_sync`. Este
router lee `changes[0].field` para clasificar cada evento y despacha al
handler correspondiente, así el router de FastAPI no necesita saber nada de
estos formatos: solo llama a `route(payload)` y actúa según
`result["event_kind"]`.

El caso "messages" (mensaje de cliente o status update) se delega tal cual
a `WhatsAppWebhookService.process_incoming_payload`, que ya sabía distinguir
esos dos sub-casos. Lo nuevo acá es echo/history/app_state_sync.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

from app.firestore_db.firebase_schemas import Messages
from app.schemas.enum_schemas import (
    HandoffMotivo,
    MessageDirection,
    MessageOrigin,
    MessageStatus,
    WebhookEventKind,
)
from app.services.firestore_services.firestore_webhook_service import (
    WhatsAppWebhookService,
    find_business,
)
from app.services.time_utils import as_utc, from_wa_timestamp

logger = logging.getLogger("whatsapp-webhook")


class WhatsAppWebhookEventRouter:
    def __init__(self) -> None:
        self._webhook_service = WhatsAppWebhookService()

    def route(self, payload: dict, business_id: str | None = None) -> dict:
        """`business_id`: lo trae el sobre del webhook por tenant, que ya
        verificó la firma con el secreto de ese negocio. None = webhook
        legado, el negocio se busca por el número."""
        try:
            entry = payload.get("entry", [{}])[0]
            changes = entry.get("changes", [{}])[0]
            field = changes.get("field")
            value = changes.get("value", {})
        except Exception:
            logger.exception("No se pudo leer field/value del payload del webhook")
            return {"event_kind": WebhookEventKind.UNKNOWN.value, "error": "parse_error"}

        if field == "smb_message_echoes" or "message_echoes" in value:
            return self._process_echo(value, payload, business_id)

        if field == "history":
            return self._process_history(value, entry.get("id"), payload, business_id)

        if field == "smb_app_state_sync":
            return self._process_app_state_sync(value, entry.get("id"), payload, business_id)

        # "messages" (o field desconocido): WhatsAppWebhookService ya
        # distingue mensaje de cliente vs. status update mirando 'value'.
        result = self._webhook_service.process_incoming_payload(payload, business_id)
        if result is None:
            return {
                "event_kind": WebhookEventKind.UNKNOWN.value,
                "error": "parse_error_or_business_not_found",
            }
        return result

    def _process_echo(self, value: dict, payload: dict, business_id: str | None = None) -> dict:
        """Mensaje enviado por un HUMANO desde la app de WhatsApp Business.

        NUNCA pasa por la IA. Pausa el bot en esa conversación y, a
        diferencia de un mensaje de cliente, no abre ni extiende la ventana
        de 24h (no toca `ultimo_mensaje_cliente_at`).
        """
        echoes = value.get("message_echoes") or []
        if not echoes:
            return {"event_kind": WebhookEventKind.ECHO.value, "error": "empty_echo_payload"}

        echo_data = echoes[0]
        wamid = echo_data.get("id")
        from_phone = echo_data.get("from")  # número del negocio
        to_phone = echo_data.get("to")  # número del cliente
        msg_type = echo_data.get("type", "text")
        # Momento real en que la encargada escribió desde el celular. Todo lo
        # que decide la pausa se fecha con esto, no con cuándo llegó la
        # tarea: Cloud Tasks no garantiza orden.
        echo_at = from_wa_timestamp(echo_data.get("timestamp")) or datetime.now(timezone.utc)

        if not wamid:
            return {"event_kind": WebhookEventKind.ECHO.value, "error": "missing_wamid"}

        # Idempotencia: Meta reintenta la entrega igual que con mensajes.
        if (
            self._webhook_service.register_event_once(
                wamid, from_phone, payload, WebhookEventKind.ECHO.value
            )
            is None
        ):
            return {"event_kind": WebhookEventKind.ECHO.value, "duplicate": True}

        business = find_business(from_phone, (value.get("metadata") or {}).get("phone_number_id"), business_id)
        if not business:
            return {
                "event_kind": WebhookEventKind.ECHO.value,
                "error": f"business_not_found:{from_phone}",
            }

        business_id = business.bussiness_id
        content, media_id, mime_type = self._webhook_service._extract_message_content(
            msg_type, echo_data
        )

        # Igual que un mensaje de cliente nuevo: si el humano le escribe a
        # un número que nunca había escrito, se crea el cliente/conversación
        # (edge case: "echo en conversación inexistente").
        customer = self._webhook_service._get_or_create_customer(
            business_id, to_phone, "Cliente Nuevo"
        )
        conversation = self._webhook_service._get_or_create_conversation(
            business_id, customer.customer_id
        )

        message = self._webhook_service._save_message(
            business_id=business_id,
            customer_id=customer.customer_id,
            conversation_id=conversation.conversation_id,
            wa_message_id=wamid,
            content=content,
            msg_type=msg_type,
            media_id=media_id,
            mime_type=mime_type,
        )
        message.role = "assistant"  # saliente, mismo rol que usa el runner ADK para lo saliente
        message.direction = MessageDirection.SALIENTE.value
        message.origin = MessageOrigin.HUMANO.value
        message.status = "sent"
        message.timestamp = echo_at
        message.update()

        # Deliberadamente NO se toca ultimo_mensaje_cliente_at: un echo no
        # es un mensaje del cliente, no debe abrir/extender la ventana 24h.
        conversation.last_message_at = datetime.now(timezone.utc)
        conversation.last_message_id = message.message_id
        # max(): un echo viejo que llega tarde no debe "rejuvenecer" la
        # última intervención de la encargada.
        previous_human_at = conversation.ultimo_mensaje_humano_at
        if previous_human_at is None or echo_at > as_utc(previous_human_at):
            conversation.ultimo_mensaje_humano_at = echo_at
        conversation.update()

        # Si el bot se reactivó DESPUÉS de este echo (el echo llegó tarde),
        # esa reactivación ya contó este silencio: no se vuelve a pausar.
        # Ver Conversations.bot_reactivado_at.
        reactivated_at = getattr(conversation, "bot_reactivado_at", None)
        # El chat de un admin nunca se pausa: si desde el número del negocio
        # se le escribe (ej. el link del catálogo), sus fotos de menú se
        # perdían mientras el bot estaba en pausa (prueba real 2026-10-19).
        from app.ai.ai_restaurant.role_resolver import resolve_user_role

        if resolve_user_role(business_id, to_phone)["role"] == "admin":
            logger.info("Echo al chat de un admin %s: no se pausa", wamid)
        elif reactivated_at is not None and echo_at < as_utc(reactivated_at):
            logger.info("Echo %s anterior a la última reactivación del bot: no se pausa", wamid)
        else:
            from app.services.conversation.handoff_service import HandoffService

            HandoffService().pause_bot(
                conversation,
                motivo=HandoffMotivo.RESPUESTA_MANUAL.value,
                at=as_utc(conversation.ultimo_mensaje_humano_at),
            )

        return {
            "event_kind": WebhookEventKind.ECHO.value,
            "business": business,
            "customer": customer,
            "conversation": conversation,
            "message": message,
        }

    @staticmethod
    def _content_key(event_kind: str, entry_id: str | None, value: dict) -> str:
        """history / smb_app_state_sync no traen un wamid único: la
        idempotencia se hace con un hash del propio contenido."""
        digest = hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24]
        return f"{event_kind}:{entry_id}:{digest}"

    def _process_history(self, value: dict, entry_id: str | None, payload: dict, business_id: str | None = None) -> dict:
        """Chats anteriores a la conexión (webhook `history` de coexistencia).

        Se guardan como Messages con origin=historial, status=completed y la
        fecha ORIGINAL del mensaje: así nunca cuentan como pendientes de
        respuesta (ver inbound_pipeline._claim_pending_messages), nunca pasan por la IA
        y nunca pausan ni reactivan el bot. El webhook ya los separó por hilo
        (ver webhook_payload.split_history): cada tarea trae un solo cliente.
        """
        kind = WebhookEventKind.HISTORY.value
        event_key = self._content_key(kind, entry_id, value)
        # reprocess_incomplete: si una tarea anterior falló a mitad de
        # camino, el reintento de Cloud Tasks la retoma (cada mensaje y
        # cliente se deduplica solo) en vez de descartarla como duplicada.
        webhook_event = self._webhook_service.register_event_once(
            event_key, None, payload, kind, reprocess_incomplete=True
        )
        if webhook_event is None:
            return {"event_kind": kind, "duplicate": True}

        metadata = value.get("metadata") or {}
        business = find_business(
            metadata.get("display_phone_number"), metadata.get("phone_number_id"), business_id
        )
        if not business:
            return {
                "event_kind": kind,
                "error": f"business_not_found:{metadata.get('phone_number_id')}",
            }
        business_id = business.bussiness_id
        business_digits = _digits(business.whatsapp_phone_number or metadata.get("display_phone_number"))
        saved = 0
        for chunk in value.get("history") or []:
            errors = chunk.get("errors") or []
            if errors:
                # 2593109: el dueño no compartió el historial. No es un fallo
                # nuestro; queda registrado y no se reintenta.
                code = errors[0].get("code")
                logger.warning("Historial no disponible para el negocio %s: code=%s", business_id, code)
                continue

            for thread in chunk.get("threads") or []:
                customer_phone = thread.get("id")
                if not customer_phone:
                    continue
                customer = self._webhook_service._get_or_create_customer(
                    business_id, customer_phone, "Cliente Nuevo"
                )
                conversation = self._webhook_service._get_or_create_conversation(
                    business_id, customer.customer_id
                )
                for item in thread.get("messages") or []:
                    if self._save_history_message(
                        business_id, business_digits, customer, conversation, item
                    ):
                        saved += 1

        _mark_processed(webhook_event, business_id)
        logger.info("Historial importado: %d mensajes (negocio %s)", saved, business_id)
        return {"event_kind": kind, "status": "history_imported", "saved": saved}

    def _save_history_message(self, business_id, business_digits, customer, conversation, item) -> bool:
        """Guarda UN mensaje del historial; False si ya existía (Meta y
        Cloud Tasks pueden repetir entregas)."""
        wamid = item.get("id")
        if not wamid:
            return False
        if Messages.collection.filter(whatsapp_message_id=wamid).get() is not None:
            return False

        msg_type = item.get("type", "text")
        content, media_id, mime_type = self._webhook_service._extract_message_content(msg_type, item)
        sent_at = from_wa_timestamp(item.get("timestamp")) or datetime.now(timezone.utc)
        from_business = bool(business_digits) and _digits(item.get("from")) == business_digits

        message = self._webhook_service._save_message(
            business_id=business_id,
            customer_id=customer.customer_id,
            conversation_id=conversation.conversation_id,
            wa_message_id=wamid,
            content=content,
            msg_type=msg_type,
            media_id=media_id,
            mime_type=mime_type,
        )
        message.role = "assistant" if from_business else "user"
        message.direction = (
            MessageDirection.SALIENTE.value if from_business else MessageDirection.ENTRANTE.value
        )
        message.origin = MessageOrigin.HISTORIAL.value
        # COMPLETED + fecha original: el flush nunca lo toma como pendiente.
        message.status = MessageStatus.COMPLETED.value
        message.timestamp = sent_at
        message.created_at = sent_at
        message.update()
        return True

    def _process_app_state_sync(self, value: dict, entry_id: str | None, payload: dict, business_id: str | None = None) -> dict:
        """Contactos de la app de WhatsApp Business (`smb_app_state_sync`).

        `add` crea el cliente o le pone nombre si no tenía; `remove` no
        borra nada (el cliente puede tener pedidos y conversaciones), se
        ignora. Nunca crea conversaciones ni toca el bot."""
        kind = WebhookEventKind.APP_STATE_SYNC.value
        event_key = self._content_key(kind, entry_id, value)
        # reprocess_incomplete: si una tarea anterior falló a mitad de
        # camino, el reintento de Cloud Tasks la retoma (cada mensaje y
        # cliente se deduplica solo) en vez de descartarla como duplicada.
        webhook_event = self._webhook_service.register_event_once(
            event_key, None, payload, kind, reprocess_incomplete=True
        )
        if webhook_event is None:
            return {"event_kind": kind, "duplicate": True}

        metadata = value.get("metadata") or {}
        business = find_business(
            metadata.get("display_phone_number"), metadata.get("phone_number_id"), business_id
        )
        if not business:
            return {
                "event_kind": kind,
                "error": f"business_not_found:{metadata.get('phone_number_id')}",
            }

        added = 0
        for item in value.get("state_sync") or []:
            if item.get("type") != "contact" or item.get("action") != "add":
                continue
            contact = item.get("contact") or {}
            phone = _digits(contact.get("phone_number"))
            if not phone:
                continue
            name = contact.get("full_name") or contact.get("first_name") or "Cliente Nuevo"
            customer = self._webhook_service._get_or_create_customer(
                business.bussiness_id, phone, name
            )
            if name != "Cliente Nuevo" and customer.name in (None, "", "Cliente Nuevo"):
                customer.name = name
                customer.updated_at = datetime.now(timezone.utc)
                customer.update()
            added += 1

        _mark_processed(webhook_event, business.bussiness_id)
        logger.info("Contactos sincronizados: %d (negocio %s)", added, business.bussiness_id)
        return {"event_kind": kind, "status": "contacts_synced", "added": added}


def _digits(value) -> str:
    """Solo los dígitos de un teléfono ("+57 300 123" -> "57300123")."""
    return "".join(c for c in str(value or "") if c.isdigit())


def _mark_processed(webhook_event, business_id: str) -> None:
    webhook_event.status = "processed"
    webhook_event.processed_at = datetime.now(timezone.utc)
    webhook_event.business_id = business_id
    webhook_event.update()
