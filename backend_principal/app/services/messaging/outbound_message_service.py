"""
Envío + persistencia del mensaje saliente de la IA.

Ni WhatsAppClient (solo envía) ni firestore_webhook_service (solo procesa lo
entrante) hacen las dos cosas juntas, así que este servicio las compone.
"""

import logging
from datetime import datetime, timezone

from app.firestore_db.firebase_schemas import Conversations, Messages
from app.schemas.enum_schemas import (
    MessageDirection,
    MessageOrigin,
    MessageRole,
    MessageStatus,
)
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient

logger = logging.getLogger("whatsapp-webhook")


class OutboundMessageService:
    def __init__(self, whatsapp_client: WhatsAppClient) -> None:
        self._whatsapp_client = whatsapp_client

    async def send_and_persist(
        self,
        business_id: str,
        customer_id: str,
        conversation_id: str,
        to_phone: str,
        text: str,
        origin: str = MessageOrigin.IA.value,
    ) -> Messages:
        wamid = await self._whatsapp_client.send_text_and_get_id(to_phone, text)
        if not wamid:
            # El motivo (código de Meta) ya lo logueó WhatsAppClient; esto
            # dice a QUIÉN le faltó la respuesta. Queda FAILED en Messages.
            logger.error(
                "La respuesta (%s) no le llegó al cliente: conversation_id=%s",
                origin,
                conversation_id,
            )

        message = Messages()
        message.business_id = business_id
        message.customer_id = customer_id
        message.conversation_id = conversation_id
        message.whatsapp_message_id = wamid
        message.role = MessageRole.ASSISTANT.value
        message.direction = MessageDirection.SALIENTE.value
        message.origin = origin
        message.type = "text"
        message.content = text
        message.status = (
            MessageStatus.COMPLETED.value if wamid else MessageStatus.FAILED.value
        )
        message.timestamp = datetime.now(timezone.utc)
        message.created_at = datetime.now(timezone.utc)
        message.save()

        conversation = Conversations.collection.get(conversation_id)
        if conversation is not None:
            conversation.last_message_at = datetime.now(timezone.utc)
            conversation.last_message_id = message.message_id
            conversation.update()

        return message
