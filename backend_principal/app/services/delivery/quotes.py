"""Cotización humana del domicilio para una dirección fuera del catálogo.

1. request_quote: la IA no reconoce la zona y el cliente confirma que no
   es ninguna del catálogo -> la conversación queda pendiente_cotizacion y
   se avisa al humano (mismo aviso que un handoff).
2. set_quote: el admin responde con el valor desde su chat -> se guarda en
   la conversación, se le escribe al cliente y se deja constancia en la
   sesión de la IA del cliente, para que al volver siga con ese valor.

Nunca se cobra una tarifa por defecto ni una calculada por la IA: sin zona
ni cotización no hay total, y sin total no hay pedido confirmado ni pago.

La respuesta del admin con formato fijo ("4000 573001234567", "domicilio 4k
para Camilo", o solo "4000" si hay un único cliente esperando) se atiende sin IA (maybe_handle_admin_quote): es el paso
que más se traba si el modelo da 429. Lo demás lo resuelve set_delivery_quote
desde el agente de admin.
"""

import re
from typing import Optional

from starlette.concurrency import run_in_threadpool

from app.ai.core.agent_runner import shared_agent_runner
from app.firestore_db.firebase_schemas import Businesses, Conversations, Customers, Messages
from app.schemas.enum_schemas import DeliveryStatus, MessageOrigin
from app.services.conversation.handoff_service import COTIZACION_DOMICILIO, HandoffService
from app.services.daily_menu.menu_diff import format_cop, format_fee
from app.services.delivery.zones import normalize
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services.messaging.outbound_message_service import OutboundMessageService
from app.services.phones import normalize_phone
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient

_QUOTE_FILLER = {"domicilio", "domicilios", "para", "a", "al", "de", "el", "valor", "cobrar", "cobra", "cuesta", "es", "y"}
# "4k", "4 mil" -> 4000. Solo enteros: "4,5k" no se adivina, lo ve la IA.
_THOUSANDS = re.compile(r"(?<![\d.,])(\d+)\s*(k|mil)\b")
_MIN_PHONE_DIGITS = 10
_MAX_FEE = 200_000  # más que esto es casi seguro un error de tipeo: que lo vea la IA


async def request_quote(
    business: Businesses, customer: Customers, conversation: Conversations, address: str
) -> bool:
    """Deja la conversación esperando cotización y avisa al humano.
    Devuelve True si el aviso salió."""
    conversation.delivery_address = address
    conversation.delivery_zone = None
    conversation.delivery_fee = None
    conversation.delivery_status = DeliveryStatus.PENDIENTE_COTIZACION.value
    FireStoreCRUDService().update(conversation)

    return await HandoffService().notify_human(
        business,
        customer,
        motivo="cotizar_domicilio",
        detalle=(
            f"Dirección fuera de las zonas: {address}. Respóndeme "
            f'"domicilio <valor> para {customer.name or customer.phone_number}".'
        ),
        kind=COTIZACION_DOMICILIO,
    )


async def set_quote(
    business: Businesses, customer: Customers, conversation: Conversations, fee: float
) -> bool:
    """Guarda el valor cotizado y se lo comunica al cliente. Devuelve True
    si el mensaje al cliente salió."""
    conversation.delivery_fee = fee
    conversation.delivery_zone = None
    conversation.delivery_status = DeliveryStatus.COTIZADO.value
    FireStoreCRUDService().update(conversation)

    price = "es gratis" if fee == 0 else f"cuesta {format_cop(fee, '-')}"
    text = f"El domicilio a {conversation.delivery_address} {price}. ¿Seguimos con tu pedido?"
    sent = await WhatsAppClient.for_business(business).send_message(customer.phone_number, text)
    # La IA del cliente no ve lo que se le manda por fuera de ella: sin esta
    # nota, cuando el cliente responda "sí" no sabría de qué se habla.
    await shared_agent_runner().append_human_message(
        business_id=business.bussiness_id,
        conversation_id=conversation.conversation_id,
        user_id=customer.customer_id,
        text=text,
        by_system=True,
    )
    return sent


def parse_quote_command(text: str) -> Optional[tuple[int, str]]:
    """"domicilio $4.000 para 3001234567" / "4k Camilo" / "4000" -> (valor,
    a quién): el teléfono normalizado, el código de usuario o el nombre tal
    cual, o "" si no dijo. None si no hay exactamente un valor."""
    plain = _THOUSANDS.sub(lambda m: str(int(m.group(1)) * 1000), (text or "").lower().replace("$", " ").replace("+", ""))
    fee, who = None, []
    plain = re.sub(r"\bgratis\b", " 0 ", plain)  # "gratis para Camilo" = 0
    for token in plain.split():
        digits = re.sub(r"[.,]", "", token)
        if digits.isdigit() and len(digits) < _MIN_PHONE_DIGITS:
            if fee is not None:
                return None
            fee = int(digits)
        elif digits.isdigit():
            who.append(normalize_phone(digits))
        elif token not in _QUOTE_FILLER:
            who.append(token)
    if fee is None or not 0 <= fee <= _MAX_FEE:
        return None
    return fee, " ".join(who)


def pending_quotes(business_id: str) -> list[tuple[Customers, Conversations]]:
    """Los clientes que hoy esperan que el equipo les cotice el domicilio."""
    pending = TenantScopedQuery.list_for_business(
        Conversations, business_id, status="active",
        delivery_status=DeliveryStatus.PENDIENTE_COTIZACION.value,
    )
    pairs = ((Customers.collection.get(c.customer_id), c) for c in pending)
    return [(customer, c) for customer, c in pairs if customer is not None]


def match_pending(
    pending: list[tuple[Customers, Conversations]], who: str
) -> list[tuple[Customers, Conversations]]:
    """Los pendientes que corresponden a lo que escribió el admin: su
    teléfono, su código de usuario o el comienzo de una palabra de su
    nombre ("seba" -> "Sebastián G"). Con "" valen todos."""
    key = normalize(who)
    if not key:
        return pending

    def matches(customer: Customers) -> bool:
        ids = {normalize(customer.phone_number), normalize(getattr(customer, "contact_phone", None) or "")}
        return key in ids or f" {key}" in f" {normalize(customer.name)}"

    return [pair for pair in pending if matches(pair[0])]


async def maybe_handle_admin_quote(
    whatsapp_client: WhatsAppClient, business: Businesses, admin: Customers, message: Messages
) -> bool:
    """True si el mensaje del admin era una cotización con formato fijo para
    un cliente que la está esperando, y ya quedó registrada; False si no
    aplica (sigue al agente de admin)."""
    if message.type != "text":
        return False
    command = parse_quote_command(message.content)
    if command is None:
        return False
    fee, who = command
    business_id = business.bussiness_id
    found = match_pending(await run_in_threadpool(pending_quotes, business_id), who)
    if len(found) != 1:
        return False  # nadie o ambiguo: el agente de admin pregunta
    customer, conversation = found[0]

    notified = await set_quote(business, customer, conversation, fee)
    text = f"Listo, domicilio de {customer.name or customer.phone_number}: {format_fee(fee, '-')}."
    if not notified:
        text += " No le pude escribir al cliente: avísale tú."
    await OutboundMessageService(whatsapp_client).send_and_persist(
        business_id=business_id,
        customer_id=admin.customer_id,
        conversation_id=message.conversation_id,
        to_phone=admin.phone_number,
        text=text,
        origin=MessageOrigin.IA.value,
    )
    return True
