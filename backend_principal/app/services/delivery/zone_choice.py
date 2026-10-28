"""El cliente eligió su barrio en la lista (ver customer_tools.ask_zone_choice).

Lo atiende el código, nunca la IA: la zona elegida pone la tarifa en la
conversación, igual que si la dirección la hubiera encontrado sola, y queda
recordada para esa dirección (resolve_delivery_fee). "Ninguno de estos"
pasa a cotización con el equipo. Después el mensaje sigue al agente con una
nota de lo que pasó, para que continúe el pedido.
"""

import logging
from typing import Optional

from app.ai.ai_restaurant.user.tools import order_tools
from app.ai.core.agent_runner import shared_agent_runner
from app.firestore_db.firebase_schemas import Businesses, Conversations, Customers, Messages
from app.schemas.enum_schemas import DeliveryStatus
from app.services.daily_menu.menu_diff import format_fee
from app.services.delivery import quotes
from app.services.delivery.zones import CHOICE_NONE, CHOICE_PREFIX, zones_for
from app.services.firestore_services.firestore_service import FireStoreCRUDService

logger = logging.getLogger("whatsapp-webhook")

_STALE = "[Nota del sistema: el cliente tocó una opción de una lista de barrios que ya no aplica. Atiéndelo normal.]"
_NONE = (
    "[Nota del sistema: el cliente dijo que su barrio no está en la lista. El equipo está cotizando "
    "el domicilio: dile que en un momento le confirman el valor.]"
)


async def maybe_handle(business: Businesses, customer: Customers, conversation: Conversations, message: Messages) -> bool:
    """Siempre False: si era una elección de barrio, la aplica y deja en
    message.content la nota para el agente (que contesta en el flush)."""
    if message.type != "interactive" or not (message.content or "").startswith(CHOICE_PREFIX):
        return False
    choice = message.content[len(CHOICE_PREFIX):]
    zones = zones_for(business.bussiness_id)

    def pick(state: dict) -> tuple[Optional[dict], dict]:
        order = state.get(order_tools.STATE_KEY)
        options = (order or {}).get("zona_opciones") or []
        if not options:
            return None, {"vigente": False}
        name = options[int(choice)] if choice.isdigit() and int(choice) < len(options) else None
        zone = next((z for z in zones if z.get("nombre") == name), None) if name else None
        order["zona_opciones"] = None
        if zone is not None:
            fresh = Conversations.collection.get(conversation.conversation_id)
            fresh.delivery_zone = zone.get("nombre")
            fresh.delivery_fee = zone.get("tarifa")
            fresh.delivery_status = DeliveryStatus.ZONA.value
            FireStoreCRUDService().update(fresh)
        ctx = order_tools._StateContext(state)
        order_tools._refresh_delivery(order, ctx)
        order_tools._totals(order)
        return {order_tools.STATE_KEY: order}, {"vigente": True, "zona": zone}

    result = await shared_agent_runner().edit_state(
        business.bussiness_id, conversation.conversation_id, customer.customer_id, pick
    )
    logger.info("Barrio elegido en la lista", extra={"fields": {"vigente": result["vigente"], "zona": bool(result.get("zona"))}})

    if not result["vigente"]:
        note = _STALE
    elif result["zona"] is not None:
        zone = result["zona"]
        note = (
            f"[Nota del sistema: el cliente eligió en la lista su barrio: {zone.get('nombre')} "
            f"(domicilio {format_fee(zone.get('tarifa'), '-')}). Sigue con lo que falte del pedido.]"
        )
    else:
        # "Ninguno de estos" (o una opción que ya no existe): cotiza un humano.
        fresh = Conversations.collection.get(conversation.conversation_id)
        await quotes.request_quote(business, customer, fresh, fresh.delivery_address or "")
        await shared_agent_runner().edit_state(
            business.bussiness_id, conversation.conversation_id, customer.customer_id, _refresh
        )
        note = _NONE

    message.content = note
    FireStoreCRUDService().update(message)
    return False


def _refresh(state: dict) -> tuple[Optional[dict], dict]:
    order = state.get(order_tools.STATE_KEY)
    if not order:
        return None, {}
    order_tools._refresh_delivery(order, order_tools._StateContext(state))
    order_tools._totals(order)
    return {order_tools.STATE_KEY: order}, {}
