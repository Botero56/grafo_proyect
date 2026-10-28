import hashlib
import logging
from datetime import datetime, timedelta, timezone
from app.firestore_db.firebase_schemas import (
    Businesses, Customers, Conversations,
    Messages, WebhookEvents, Configurations
)
from app.schemas.enum_schemas import WebhookEventKind

from app.config import firestore_settings
from app.services.firestore_services import firestore_service
from app.services.firestore_services.firestore_service import ensure_firestore_connection
from app.services.time_utils import as_utc, from_wa_timestamp
from typing import Dict

# Lo que queda en Messages.content de un carrito del catálogo hasta que
# CatalogOrderHandler lo cambia por el resumen del pedido.
CART_PLACEHOLDER = "[Pedido desde el carrito del catálogo]"

logger = logging.getLogger("whatsapp-webhook")



def find_business(display_phone_number, phone_number_id=None, business_id=None):
    """Negocio dueño del número que recibió (o envió) el evento.

    Con `business_id` (webhook por tenant, firma ya verificada con el
    secreto de ese negocio) se usa ese negocio, y solo si el
    `phone_number_id` del evento es el suyo: un id no autoriza a escribir
    en las conversaciones de otro número.

    Primero por el número visible, que es como se resolvió siempre. Si no
    aparece, por el `phone_number_id` de Meta: el onboarding por Embedded
    Signup lo guarda en el negocio, y el número visible que tengamos
    registrado puede venir con otro formato."""
    if business_id:
        business = Businesses.collection.get(business_id)
        if business is not None and phone_number_id and business.whatsapp_phone_number_id != phone_number_id:
            logger.error("El evento del negocio %s trae otro phone_number_id; se descarta", business_id)
            return None
        return business
    business = None
    if display_phone_number:
        business = Businesses.collection.filter(
            whatsapp_phone_number=display_phone_number
        ).get()
    if business is None and phone_number_id:
        business = Businesses.collection.filter(
            whatsapp_phone_number_id=phone_number_id
        ).get()
    return business


def find_active_conversation(business_id, customer_id):
    """La conversación abierta del cliente en ese negocio, o None. Una
    cerrada no cuenta: el próximo mensaje abre una nueva."""
    return Conversations.collection.filter(
        business_id=business_id, customer_id=customer_id, status="active"
    ).get()


def wrote_within_24h(business_id, phone, now: datetime) -> bool:
    """True si ese teléfono le escribió al negocio en las últimas 24 h.

    Fuera de esa ventana WhatsApp no entrega mensajes de texto libre
    iniciados por el negocio (los acepta con 200 y después los marca como
    fallidos): hace falta una plantilla aprobada."""
    customer = Customers.collection.filter(business_id=business_id, phone_number=phone).get()
    if customer is None:
        return False
    conversation = find_active_conversation(business_id, customer.customer_id)
    if conversation is None or conversation.ultimo_mensaje_cliente_at is None:
        return False
    return (now - as_utc(conversation.ultimo_mensaje_cliente_at)) <= timedelta(hours=24)


class WhatsAppWebhookService:

    def __init__(self,credentials:Dict[str,str] = firestore_settings):

        self.provider = "whatsapp"
        ensure_firestore_connection(credentials)


    def process_incoming_payload(self, payload: dict, business_id: str | None = None):
        """
        Orquestador principal que popula la base de datos basándose en el webhook.
        """
        # 1. Extraer datos básicos del webhook de WhatsApp
        try:
            entry = payload.get('entry', [{}])[0]
            changes = entry.get('changes', [{}])[0]
            value = changes.get('value', {})
            
            # Si no hay mensajes, puede ser un status (delivered, read, etc.)
            if 'messages' not in value:
                return self._handle_status_update(value, payload)

            message_data = value.get('messages', [{}])[0]
            contact_data = value.get('contacts', [{}])[0]
            metadata = value.get('metadata', {})

            # Datos clave
            wa_message_id = message_data.get('id')
            # Teléfono del cliente, o su BSUID si escribe con nombre de
            # usuario (Meta no manda `from`): sin esto todos esos clientes
            # caían en el mismo registro (prueba real 2026-10-19).
            from_phone = message_data.get('from') or message_data.get('from_user_id')
            to_phone = metadata.get('display_phone_number') # Teléfono de la empresa
            message_type = message_data.get('type', 'text')
            message_text, media_id, mime_type = self._extract_message_content(
                message_type, message_data
            )
            customer_name = contact_data.get('profile', {}).get('name', "Cliente Nuevo")

            # WhatsApp manda el momento real en que el cliente escribió el
            # mensaje (epoch en segundos, como string) en message_data
            # ["timestamp"] -- separado de cuándo nos llega el webhook. Si
            # Meta o el teléfono del cliente lo entregan tarde (red
            # inestable, reintentos), esa diferencia sirve para que la IA
            # sepa que el mensaje no es "de ahora mismo" (ver
            # message_delay_seconds más abajo). OJO: hoy nadie lo consume --
            # solo lo usaba el monolito, y flush_conversation todavía no le
            # pasa esa nota a la IA.
            message_delay_seconds = None
            wa_sent_at = from_wa_timestamp(message_data.get('timestamp'))
            if wa_sent_at is not None:
                message_delay_seconds = (
                    datetime.now(timezone.utc) - wa_sent_at
                ).total_seconds()
            phone_number_id = metadata.get('phone_number_id')

        except Exception:
            logger.exception("Error parseando el payload del mensaje")
            return None

        # 2-3. Idempotencia + auditoría cruda: si ya procesamos este
        # wa_message_id, no repetir nada (WhatsApp reintenta la entrega del
        # mismo mensaje varias veces).
        webhook_event = self.register_event_once(
            wa_message_id, to_phone, payload, reprocess_incomplete=True
        )
        if webhook_event is None:
            return {"duplicate": True, "event_kind": WebhookEventKind.MESSAGE.value}

        # 4. Identificar el Negocio (Business)
        business = find_business(to_phone, phone_number_id, business_id)
        if not business:
            webhook_event.status = "failed"
            webhook_event.error = f"Negocio con teléfono {to_phone} no encontrado."
            webhook_event.update()
            logger.error("Mensaje descartado: ningún negocio tiene el teléfono ***%s", (to_phone or "")[-4:])
            return None
        
        business_id = business.bussiness_id

        # 5. Identificar o Crear al Cliente (Customer)
        customer = self._get_or_create_customer(business_id, from_phone, customer_name)

        # 6. Identificar o Crear la Conversación (Conversation)
        conversation = self._get_or_create_conversation(business_id, customer.customer_id)

        # 7. Guardar el Mensaje (Message). Si es el reintento de una ingesta
        # que falló después de guardarlo, se reutiliza en vez de duplicarlo
        # (la IA vería el texto dos veces).
        message = Messages.collection.filter(whatsapp_message_id=wa_message_id).get() or self._save_message(
            business_id=business_id,
            customer_id=customer.customer_id,
            conversation_id=conversation.conversation_id,
            wa_message_id=wa_message_id,
            content=message_text,
            msg_type=message_type,
            media_id=media_id,
            mime_type=mime_type,
        )

        # 8. Actualizar la conversación con el último mensaje. También
        # marcamos ultimo_mensaje_cliente_at -- esto sí abre/extiende la
        # ventana de 24h de WhatsApp, a diferencia de un echo de humano (ver
        # WhatsAppWebhookEventRouter._process_echo, que deliberadamente NO
        # toca este campo).
        conversation.last_message_at = datetime.now(timezone.utc)
        conversation.last_message_id = message.message_id
        conversation.ultimo_mensaje_cliente_at = datetime.now(timezone.utc)
        conversation.update()

        # Marcar evento como procesado
        webhook_event.status = "processed"
        webhook_event.processed_at = datetime.now(timezone.utc)
        webhook_event.business_id = business_id
        webhook_event.update()

        # Retornamos el objeto con toda la info para que la IA sepa dónde responder
        return {
            "business": business,
            "customer": customer,
            "conversation": conversation,
            "message": message,
            "message_delay_seconds": message_delay_seconds,
            # Momento real en que el cliente escribió (None si Meta no lo
            # mandó). Con esto se decide la pausa del bot frente a los echoes
            # sin depender del orden en que Cloud Tasks entrega los eventos.
            "wa_sent_at": wa_sent_at,
            # Carrito del catálogo (mensaje `order`), None en los demás.
            "order": message_data.get("order") if message_type == "order" else None,
            "event_kind": WebhookEventKind.MESSAGE.value,
        }

    def register_event_once(
        self, event_key, to_phone, payload, event_kind=WebhookEventKind.MESSAGE.value,
        *, reprocess_incomplete=False,
    ):
        """Guarda el evento crudo salvo que esa clave ya exista -- Meta
        reintenta las entregas. Devuelve None si era un duplicado.

        `reprocess_incomplete`: un evento que quedó "received" es una
        ingesta que falló a mitad de camino (process_incoming_payload lo
        marca "processed" al terminar). Con esto el reintento de Cloud Tasks
        lo vuelve a procesar en vez de descartarlo como duplicado y perder
        el mensaje. Solo para mensajes de cliente: echoes y eventos de
        auditoría nunca pasan a "processed"."""
        existing = WebhookEvents.collection.get(event_key)
        if existing is None:
            return self.save_webhook_event(event_key, to_phone, payload, event_kind=event_kind)
        if reprocess_incomplete and existing.status == "received":
            logger.warning("Reprocesando el evento %s, que quedó a medias", event_key)
            return existing
        return None

    def save_webhook_event(self, wa_id, to_phone, payload, event_kind=WebhookEventKind.MESSAGE.value):
        event = WebhookEvents()
        event.webhook_event_id = wa_id # Idempotencia
        event.event_id = wa_id
        event.provider = self.provider
        event.event_type = "message"
        event.event_kind = event_kind
        event.raw_payload = payload
        event.status = "received"
        event.received_at = datetime.now(timezone.utc)
        # create(): dos entregas simultáneas del mismo evento -> solo una sigue.
        return event if firestore_service.create_if_absent(event) else None

    def _get_or_create_customer(self, business_id, phone, name):
        """Id determinista `{business_id}_{hash del teléfono}` creado con
        create(): si llegan varios mensajes de un cliente nuevo a la vez, uno
        lo crea y los demás leen ese mismo documento. Hash y no el teléfono:
        el id sale en logs y en ids de conversación. Los clientes de antes
        (id automático) se siguen encontrando por la consulta."""
        doc_id = f"{business_id}_" + hashlib.sha256(f"{business_id}:{phone}".encode()).hexdigest()[:24]
        customer = Customers.collection.get(doc_id) or Customers.collection.filter(
            business_id=business_id, phone_number=phone
        ).get()
        if not customer:
            customer = Customers()
            customer.customer_id = doc_id
            customer.business_id = business_id
            customer.name = name
            customer.phone_number = phone
            customer.whatsapp_user_id = phone
            customer.address = ""
            customer.notes = ""
            customer.status = "active"
            customer.created_at = datetime.now(timezone.utc)
            customer.updated_at = datetime.now(timezone.utc)
            if not firestore_service.create_if_absent(customer):
                customer = Customers.collection.get(doc_id)  # otro proceso lo creó primero
        return customer

    def _get_or_create_conversation(self, business_id, customer_id):
        """La conversación activa, o una nueva con id `{customer_id}_c{n}`
        (n = conversaciones que ya tuvo) creada con create(): dos mensajes
        simultáneos calculan el mismo id y solo uno la crea."""
        conversation = find_active_conversation(business_id, customer_id)
        if conversation:
            return conversation

        taken = len(list(Conversations.collection.filter(business_id=business_id, customer_id=customer_id).fetch()))
        for n in range(taken, taken + 5):
            conversation = Conversations()
            conversation.conversation_id = f"{customer_id}_c{n}"
            conversation.business_id = business_id
            conversation.customer_id = customer_id
            conversation.channel = "whatsapp"
            conversation.status = "active"
            conversation.started_at = datetime.now(timezone.utc)
            conversation.last_message_at = datetime.now(timezone.utc)
            # Aunque BooleanField(default=True) hace que el documento se
            # guarde con bot_activo=true, fireo solo aplica ese default en
            # la SERIALIZACIÓN (to_dict()/save()) -- el atributo en memoria
            # de ESTE objeto Python sigue en None hasta que se relea desde
            # Firestore. Si no lo seteamos acá explícitamente,
            # `conversation.bot_activo` da None (falsy) inmediatamente
            # después de crear la conversación, y el webhook trataría a
            # todo cliente nuevo como si el bot estuviera pausado.
            conversation.bot_activo = True
            if firestore_service.create_if_absent(conversation):
                return conversation
            existing = Conversations.collection.get(conversation.conversation_id)
            if existing is not None and existing.status == "active":
                return existing  # otro proceso la creó primero
        raise RuntimeError(f"No se pudo abrir conversación para {customer_id}")

    def _save_message(self, business_id, customer_id, conversation_id, wa_message_id, content, msg_type, media_id=None, mime_type=None):
        msg = Messages()
        msg.business_id = business_id
        msg.customer_id = customer_id
        msg.conversation_id = conversation_id
        msg.whatsapp_message_id = wa_message_id
        msg.role = "user"
        msg.type = msg_type
        msg.content = content
        if media_id:
            msg.media_id = media_id
        if mime_type:
            msg.mime_type = mime_type
        msg.status = "received"
        msg.timestamp = datetime.now(timezone.utc)
        msg.created_at = datetime.now(timezone.utc)
        if not wa_message_id:
            msg.save()
            return msg
        # Id derivado del wamid y create(): una entrega duplicada que se
        # procesa a la vez guarda UN mensaje (el wamid trae "/" de base64).
        msg.message_id = "wa_" + hashlib.sha256(wa_message_id.encode()).hexdigest()[:32]
        if firestore_service.create_if_absent(msg):
            return msg
        return Messages.collection.get(msg.message_id)

    # Tipos de mensaje entrante cuyo contenido "de media" vive en
    # message_data[msg_type] = {"id": ..., "mime_type": ..., "caption"?: ...}
    _MEDIA_MESSAGE_TYPES = {"image", "audio", "video", "document", "sticker"}

    _MEDIA_PLACEHOLDER_TEXT = {
        "image": "[Imagen recibida]",
        "audio": "[Audio recibido]",
        "video": "[Video recibido]",
        "document": "[Documento recibido]",
        "sticker": "[Sticker recibido]",
    }

    def _extract_message_content(self, msg_type: str, message_data: dict):
        """Devuelve (content, media_id, mime_type) según el tipo de mensaje.

        Para texto, content es el body tal cual. Para media, WhatsApp no
        manda el archivo sino un media_id (la url real hay que pedirla
        aparte, ver WhatsAppClient.download_media). content queda como el
        caption si lo hay, o un placeholder legible para que el agente no
        reciba un string vacío.
        """
        if msg_type == "text":
            return message_data.get("text", {}).get("body", ""), None, None

        if msg_type == "interactive":
            # Respuesta a un mensaje interactivo de botones (ver
            # WhatsAppClient.send_interactive_buttons). content = el ID del
            # botón (ej. "menu:{daily_menu_id}:confirm"), no el título -- es
            # lo que el orchestrator necesita para actuar, el título es solo
            # para mostrarle al admin, no hace falta persistirlo aparte.
            interactive = message_data.get("interactive") or {}
            # Botón o fila de una lista (send_interactive_list): el mismo id.
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            return reply.get("id", ""), None, None

        if msg_type == "order":
            # Carrito del catálogo: lo atiende CatalogOrderHandler (con
            # "order" del resultado), que cambia este texto por el resumen.
            return CART_PLACEHOLDER, None, None

        if msg_type == "location":
            # Ubicación compartida: a texto, para que el agente la tome como
            # dirección de entrega (antes llegaba vacía y se ignoraba).
            loc = message_data.get("location") or {}
            place = ", ".join(p for p in (loc.get("name"), loc.get("address")) if p)
            lat, lng = loc.get("latitude"), loc.get("longitude")
            coords = f"https://maps.google.com/?q={lat},{lng}" if lat is not None and lng is not None else ""
            return " ".join(p for p in ("[Ubicación compartida]", place, coords) if p), None, None

        if msg_type not in self._MEDIA_MESSAGE_TYPES:
            return "", None, None

        media = message_data.get(msg_type) or {}
        content = media.get("caption") or self._MEDIA_PLACEHOLDER_TEXT.get(msg_type, "")
        return content, media.get("id"), media.get("mime_type")

    def _handle_status_update(self, value, payload):
        """Actualiza el estado de mensajes SALIENTES (sent/delivered/read/
        failed) por su wamid. Nunca toca contenido ni pasa por la IA --
        WhatsApp puede mandar varios status distintos para el mismo wamid
        (sent, luego delivered, luego read), así que la idempotencia se
        indexa por (wamid, status), no solo por wamid.
        """
        statuses = value.get("statuses") or []
        results = []
        for status_data in statuses:
            wa_message_id = status_data.get("id")
            wa_status = status_data.get("status")
            if not wa_message_id or not wa_status:
                continue

            event_key = f"status:{wa_message_id}:{wa_status}"
            if self.register_event_once(event_key, None, payload, WebhookEventKind.STATUS.value) is None:
                results.append({"wamid": wa_message_id, "wa_status": wa_status, "duplicate": True})
                continue

            message = Messages.collection.filter(whatsapp_message_id=wa_message_id).get()
            if message is not None:
                message.status = wa_status
                message.update()

            results.append({"wamid": wa_message_id, "wa_status": wa_status, "duplicate": False})

        return {
            "event_kind": WebhookEventKind.STATUS.value,
            "status": "status_update_processed",
            "statuses": results,
        }