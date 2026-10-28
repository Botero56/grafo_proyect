"""Detector de mensajes sin contestar (alerta "el bot no está respondiendo").

Cloud Monitoring no sabe el horario de cada negocio, así que una alerta por
"ningún Turno de IA en 30 min" sonaría cada noche y cada domingo. Esto mide
lo que importa: un cliente que escribió a un negocio abierto y lleva más de
UNANSWERED_AFTER sin respuesta. Un negocio sin clientes no es una caída.

Lo dispara Cloud Scheduler cada 5 min (POST /api/v1/admin/jobs/check-unanswered);
cada hallazgo es un ERROR con ops_event=unanswered_messages, que tiene su
propia alerta (ops/alerts.sh).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.firestore_db.firebase_schemas import Businesses, Conversations, Customers
from app.schemas.enum_schemas import is_business_active
from app.services import opening_hours, privacy
from app.services.messaging import inbound_pipeline
from app.services.time_utils import as_utc

logger = logging.getLogger("whatsapp-webhook")

# Un turno normal tarda segundos (debounce + IA). Pasados los 10 min del
# claim (_CLAIM_MAX_AGE) el mensaje ya no se contesta: la ventana útil es
# [3, 10) min, más larga que el intervalo del job, así no se escapa ninguno.
UNANSWERED_AFTER = timedelta(minutes=3)


def check_unanswered(now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    recent = Conversations.collection.filter(
        "ultimo_mensaje_cliente_at", ">=", now - inbound_pipeline._CLAIM_MAX_AGE
    ).fetch()
    businesses: dict[str, Optional[Businesses]] = {}
    checked, findings = 0, []
    for conversation in recent:
        # Pausada por handoff: la contesta un humano, no el bot.
        if conversation.status != "active" or conversation.bot_activo is False:
            continue
        business_id = conversation.business_id
        if business_id not in businesses:
            businesses[business_id] = Businesses.collection.get(business_id)
        business = businesses[business_id]
        if business is None or not is_business_active(business) or not opening_hours.status(business_id, now=now)["open"]:
            continue
        checked += 1
        stale = [
            m for m in inbound_pipeline._unanswered_messages(conversation) if as_utc(m.created_at) <= now - UNANSWERED_AFTER
        ]
        if not stale:
            continue
        # Esperando que toque "Acepto" (privacy.gate): no es un mensaje olvidado.
        customer = Customers.collection.get(conversation.customer_id)
        if getattr(customer, "data_consent_asked_at", None) and not privacy.has_consent(customer):
            continue
        finding = {"business_id": business_id, "conversation_id": conversation.conversation_id, "messages": len(stale)}
        findings.append(finding)
        logger.error(
            "%d mensaje(s) sin respuesta hace más de %d min (negocio %s, conversación %s)",
            len(stale),
            UNANSWERED_AFTER.seconds // 60,
            business_id,
            conversation.conversation_id,
            extra={"fields": {"ops_event": "unanswered_messages", **finding}},
        )
    return {"checked": checked, "unanswered": findings}
