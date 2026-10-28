"""Autorización de datos personales (Ley 1581 de 2012) de los clientes.

- Se pide ANTES de atender: el primer mensaje de un cliente sin autorización
  no pasa por la IA; se le mandan [Acepto] [No acepto] con el enlace a la
  política (privacy_page.py). Una sola vez por cliente en cada negocio.
- Acepto: queda la fecha y la versión, y el bot contesta lo que había escrito
  (sus mensajes quedaron pendientes; el flush los recoge con una nota).
- No acepto: se borra lo que se guardó de él (mensajes, conversaciones,
  eventos del webhook y su ficha si no tiene pedidos) y se le dice cómo pedir.
- El botón lo atiende el código (gate), nunca la IA. El responsable es el
  negocio; 3B Technology, el encargado.

Cambiar el texto de la política = subir POLICY_VERSION: a todos se les vuelve
a pedir la próxima vez que escriban.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.firestore_db.firebase_schemas import (
    Businesses,
    Configurations,
    Conversations,
    Customers,
    Messages,
    Orders,
    WebhookEvents,
)
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services.privacy_page import POLICY_VERSION, policy_url
from app.services.time_utils import as_utc
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient

logger = logging.getLogger("whatsapp-webhook")

CHOICE_PREFIX = "datos:"
ACCEPT = f"{CHOICE_PREFIX}acepto"
DECLINE = f"{CHOICE_PREFIX}no"
# Si sigue escribiendo sin tocar el botón, se le repite como mucho así de seguido.
_ASK_AGAIN_AFTER = timedelta(minutes=2)
# Eventos del webhook que se revisan al borrar (llegan en segundos; esto sobra).
_EVENTS_WINDOW = timedelta(days=2)

ACCEPTED_NOTE = (
    "[Nota del sistema: el cliente aceptó la política de datos tocando 'Acepto'. Atiéndelo: contesta lo que "
    "había escrito antes; si no había escrito nada más, salúdalo y pregúntale en qué le ayudas. Si estaba "
    "confirmando un pedido, llama confirm_order.]"
)


def has_consent(customer: Optional[Customers]) -> bool:
    return getattr(customer, "data_consent_version", None) == POLICY_VERSION


def _business_name(business: Businesses, configuration: Optional[Configurations]) -> str:
    return (getattr(configuration, "restaurant_name", None) or business.name or "el negocio").strip()


async def ask(business_id: str, customer_id: str) -> bool:
    """Manda el mensaje con [Acepto] [No acepto]. True si salió."""
    business = Businesses.collection.get(business_id)
    customer = Customers.collection.get(customer_id)
    if business is None or customer is None:
        return False
    configuration = TenantScopedQuery.get_one_for_business(Configurations, business_id)
    body = (
        f"¡Hola! Te atiende el asistente de {_business_name(business, configuration)}. Para ayudarte con tu "
        "pedido guardamos tu nombre, teléfono, dirección y esta conversación, solo para atenderte y entregar "
        "tus pedidos. Puedes pedir que los corrijamos o borremos cuando quieras.\n\n"
        f"Política de datos: {policy_url(business_id)}\n\n¿Aceptas?"
    )
    wamid = await WhatsAppClient.for_business(business).send_interactive_buttons(
        customer.phone_number, body, [{"id": ACCEPT, "title": "✅ Acepto"}, {"id": DECLINE, "title": "No acepto"}]
    )
    return bool(wamid)


async def gate(business: Businesses, customer: Customers, message: Messages) -> bool:
    """True = seguir atendiendo este mensaje (ya autorizó, o acaba de aceptar y
    message.content quedó con la nota para el agente). False = cortar aquí:
    se le pidió la autorización o se borraron sus datos."""
    if has_consent(customer):
        return True
    crud = FireStoreCRUDService()
    click = message.content if message.type == "interactive" and (message.content or "").startswith(CHOICE_PREFIX) else None
    if click == ACCEPT:
        customer.data_consent_at = datetime.now(timezone.utc)
        customer.data_consent_version = POLICY_VERSION
        crud.update(customer)
        message.content = ACCEPTED_NOTE
        crud.update(message)
        logger.info("Política de datos aceptada", extra={"fields": {"business_id": business.bussiness_id}})
        return True
    if click == DECLINE:
        phone = customer.phone_number
        await forget_customer(business, customer)
        await WhatsAppClient.for_business(business).send_message(
            phone,
            "Entendido, no guardamos tus datos y borramos esta conversación. Si quieres hacer un pedido, "
            f"comunícate directamente con el negocio{f' al {business.phone}' if business.phone else ''}. "
            "Si cambias de opinión, escríbenos de nuevo.",
        )
        return False

    # Sin autorización: su mensaje queda guardado sin contestar (lo contesta
    # el bot si acepta) y se le pide, sin repetirlo en cada mensaje.
    asked = getattr(customer, "data_consent_asked_at", None)
    now = datetime.now(timezone.utc)
    if asked is None or now - as_utc(asked) >= _ASK_AGAIN_AFTER:
        if await ask(business.bussiness_id, customer.customer_id):
            customer.data_consent_asked_at = now
            crud.update(customer)
    return False


async def forget_customer(business: Businesses, customer: Customers) -> dict:
    """Borra lo que se guardó del cliente que no aceptó. Sus pedidos (y por
    eso su ficha) se conservan si los tiene: la ley contable obliga."""
    crud = FireStoreCRUDService()
    business_id, customer_id, phone = business.bussiness_id, customer.customer_id, customer.phone_number or ""
    messages = list(Messages.collection.filter(business_id=business_id, customer_id=customer_id).fetch())
    conversations = list(Conversations.collection.filter(business_id=business_id, customer_id=customer_id).fetch())
    since = datetime.now(timezone.utc) - _EVENTS_WINDOW
    # ponytail: escanea los eventos de los últimos 2 días (solo rango: sin índice
    # compuesto) buscando su número en el payload; con mucho volumen, guardar
    # customer_id en WebhookEvents y filtrar por él.
    events = [
        e
        for e in WebhookEvents.collection.filter("received_at", ">=", since).fetch()
        if e.business_id == business_id and phone and phone in str(getattr(e, "raw_payload", "") or "")
    ]
    for doc in (*messages, *conversations, *events):
        crud.delete(doc)
    has_orders = Orders.collection.filter(business_id=business_id, customer_id=customer_id).get() is not None
    if not has_orders:
        crud.delete(customer)
    counts = {"messages": len(messages), "conversations": len(conversations), "events": len(events), "customer": not has_orders}
    logger.info("Datos de un cliente borrados (no aceptó la política)", extra={"fields": {"business_id": business_id, **counts}})
    return counts
