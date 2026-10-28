"""Incidencias de entrega: el domiciliario no puede entregar una comanda
(no abren, portería no responde, dirección errada...).

1. open_incident: valida la comanda (de hoy, en estado de entrega, no es del
   propio domiciliario), crea o reutiliza la incidencia (una activa por
   comanda, en transacción), le escribe al cliente y agenda el temporizador.
2. on_timeout (Cloud Task): si el cliente no ha resuelto nada, le escribe
   otra vez o escala al admin. Revisa el estado antes de actuar: un
   temporizador viejo o repetido no hace nada.
   Liquidar la comanda (tracking.settle) también la deja resuelta: el admin
   no tiene que cerrarla.

3. resolve_from_customer: lo que el cliente decide (lo interpreta su IA con
   la tool responder_incidencia, ver customer_tools): una instrucción clara
   le llega al domiciliario y la incidencia queda resuelta; cancelar,
   cambiar el cobro u "otro número" van al admin, nunca los decide la IA.
Nunca se le comparte al domiciliario el teléfono del cliente: solo al admin.
Cada transición deja un evento en el status_history del pedido.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from starlette.concurrency import run_in_threadpool

from app.ai.core.agent_runner import shared_agent_runner
from app.firestore_db.firebase_schemas import Businesses, Configurations, Customers, Incidents, Orders
from app.schemas.enum_schemas import DeliveryEventSource, IncidentStatus, MessageOrigin, OrderStatus
from app.services.conversation.handoff_service import ATENCION_CLIENTE, HandoffService, local_phone, phone_for_humans
from app.services.delivery import tracking
from app.services.firestore_services import firestore_service
from app.services.firestore_services.firestore_webhook_service import find_active_conversation, wrote_within_24h
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services.messaging.outbound_message_service import OutboundMessageService
from app.services.phones import normalize_phone
from app.services.tasks import cloud_tasks_client
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient

logger = logging.getLogger("whatsapp-webhook")

DEFAULT_WAIT_MIN = 4
DEFAULT_RETRIES = 1
ACTIVE = tracking.ACTIVE_INCIDENTS
# Solo se reportan comandas que siguen en camino (no entregadas ni cerradas).
_DELIVERABLE = (OrderStatus.EN_COCINA.value, OrderStatus.PRESUNTO_ENTREGADO.value, OrderStatus.CON_RECLAMO.value)


def _settings(configuration: Optional[Configurations]) -> tuple[int, int]:
    wait = getattr(configuration, "incidencia_espera_min", None) or DEFAULT_WAIT_MIN
    retries = getattr(configuration, "incidencia_reintentos", None)
    return int(wait), int(DEFAULT_RETRIES if retries is None else retries)


def _first_message(code: int) -> str:
    return (
        f"Hola 👋 El domiciliario de tu pedido #{code} está en tu dirección y no ha podido "
        "entregártelo. ¿Puedes salir a recibirlo o decirnos qué hacer?"
    )


def _retry_message(code: int) -> str:
    return (
        f"¿Sigues ahí? El domiciliario de tu pedido #{code} sigue esperando en tu dirección. "
        "Respóndenos para poder entregártelo."
    )


def _log(incident: Incidents, transition: str) -> None:
    logger.info(
        "Incidencia %s: %s",
        incident.incident_id,
        transition,
        extra={"fields": {"incident_id": incident.incident_id, "order_id": incident.order_id, "status": incident.status, "attempts": incident.attempts}},
    )


def _record(incident: Incidents, event_type: str, actor: str, now: datetime, notes: Optional[str] = None) -> None:
    order = Orders.collection.get(incident.order_id)
    if order is not None:
        tracking._event(order, event_type, DeliveryEventSource.INCIDENCIA.value, actor, order.status, now, notes).save()
    _log(incident, event_type)


async def send_to_customer(business: Businesses, customer_id: Optional[str], text: str) -> bool:
    """Mensaje a un cliente o domiciliario por su conversación activa (queda
    en Messages). True si WhatsApp lo aceptó."""
    customer = Customers.collection.get(customer_id) if customer_id else None
    if customer is None:
        return False
    conversation = find_active_conversation(business.bussiness_id, customer.customer_id)
    client = WhatsAppClient.for_business(business)
    if conversation is None:
        return await client.send_message(customer.phone_number, text)
    message = await OutboundMessageService(client).send_and_persist(
        business_id=business.bussiness_id,
        customer_id=customer.customer_id,
        conversation_id=conversation.conversation_id,
        to_phone=customer.phone_number,
        text=text,
        origin=MessageOrigin.IA.value,
    )
    return bool(message.whatsapp_message_id)


async def _tell_customer(business: Businesses, incident: Incidents, text: str) -> bool:
    sent = await send_to_customer(business, incident.customer_id, text)
    conversation = find_active_conversation(business.bussiness_id, incident.customer_id)
    if conversation is not None:
        # Su IA no ve lo que se le manda por fuera: sin esta nota, cuando
        # conteste "ya bajo" no sabría de qué se habla (igual que quotes).
        await shared_agent_runner().append_human_message(
            business_id=business.bussiness_id,
            conversation_id=conversation.conversation_id,
            user_id=incident.customer_id,
            text=f"[Incidencia de entrega del pedido #{incident.daily_code}] {text}",
            by_system=True,
        )
    return sent


def _create_or_reuse(order: Orders, courier: dict, courier_customer_id: str, customer: Customers, motivo: str, now: datetime) -> tuple[Incidents, bool]:
    """Una incidencia activa por comanda: si ya hay una, se devuelve esa."""

    def _run(t):
        fresh = Orders.collection.get(order.order_id, transaction=t)
        current = Incidents.collection.get(fresh.active_incident_id, transaction=t) if fresh.active_incident_id else None
        if current is not None and current.status in ACTIVE:
            return current, False
        incident = Incidents(
            incident_id=f"{order.order_id}_{int(now.timestamp())}",
            business_id=order.business_id,
            order_id=order.order_id,
            daily_code=order.daily_code,
            service_date=order.service_date,
            courier_phone=normalize_phone(courier.get("phone")),
            courier_customer_id=courier_customer_id,
            customer_id=customer.customer_id,
            customer_phone=customer.phone_number,
            motivo=" ".join((motivo or "").split())[:300],
            status=IncidentStatus.ESPERANDO_CLIENTE.value,
            attempts=1,
            created_at=now,
            updated_at=now,
        )
        incident.save(transaction=t)
        fresh.active_incident_id = incident.incident_id
        fresh.save(transaction=t)
        return incident, True

    return firestore_service.run_in_transaction(_run)


async def open_incident(
    business: Businesses, courier: dict, courier_customer_id: str, code: int, motivo: str, now: Optional[datetime] = None
) -> dict:
    """Abre (o reutiliza) la incidencia de la comanda #code de hoy.

    Returns: {"ok": True, "texto": ...} para el domiciliario, o {"ok": False,
    "reason": "no_existe" | "ya_entregada" | "es_tu_pedido"}."""
    now = now or datetime.now(timezone.utc)
    business_id = business.bussiness_id
    order = await run_in_threadpool(tracking.order_by_code, business_id, code, now)
    if order is None:
        return {"ok": False, "reason": "no_existe"}
    if order.status not in _DELIVERABLE:
        return {"ok": False, "reason": "ya_entregada"}
    if await run_in_threadpool(tracking.is_own_order, order, courier.get("phone")):
        return {"ok": False, "reason": "es_tu_pedido"}
    customer = Customers.collection.get(order.customer_id) if order.customer_id else None
    if customer is None:
        return {"ok": False, "reason": "no_existe"}

    incident, created = await run_in_threadpool(_create_or_reuse, order, courier, courier_customer_id, customer, motivo, now)
    if not created:
        if incident.status == IncidentStatus.ESCALADA.value:
            return {"ok": True, "texto": f"La novedad del pedido #{code} ya está con el administrador. Espera sus instrucciones."}
        return {"ok": True, "texto": f"Ya le escribí al cliente del pedido #{code}. Te aviso apenas responda."}

    await run_in_threadpool(_record, incident, "incidencia_abierta", incident.courier_phone, now, incident.motivo)
    configuration = TenantScopedQuery.get_one_for_business(Configurations, business_id)
    wait, _ = _settings(configuration)
    if not await run_in_threadpool(wrote_within_24h, business_id, customer.phone_number, now):
        # WhatsApp no le entrega texto libre: sin plantilla para el cliente,
        # que lo llame una persona de una vez.
        await escalate(business, incident, "No le pudimos escribir al cliente (lleva más de 24 h sin escribir).", now)
        return {"ok": True, "texto": f"No le puedo escribir al cliente del pedido #{code}. Ya avisé al administrador: espera sus instrucciones."}

    if not await _tell_customer(business, incident, _first_message(code)):
        await escalate(business, incident, "No se le pudo enviar el mensaje al cliente.", now)
        return {"ok": True, "texto": f"No pude escribirle al cliente del pedido #{code}. Ya avisé al administrador: espera sus instrucciones."}
    await run_in_threadpool(cloud_tasks_client.enqueue_incident_timeout, incident.incident_id, 1, wait * 60)
    return {
        "ok": True,
        "texto": f"Listo, le escribí al cliente del pedido #{code}. Si no responde en {wait} min, aviso al administrador.",
    }


async def escalate(business: Businesses, incident: Incidents, reason: str, now: Optional[datetime] = None) -> bool:
    """Pasa la incidencia a escalada (si nadie la resolvió antes) y avisa al
    admin y al domiciliario. False si ya no estaba esperando al cliente."""
    now = now or datetime.now(timezone.utc)
    if not await run_in_threadpool(
        firestore_service.compare_and_set, incident, "status", IncidentStatus.ESPERANDO_CLIENTE.value, IncidentStatus.ESCALADA.value
    ):
        return False
    incident.status, incident.escalated_at = IncidentStatus.ESCALADA.value, now
    await run_in_threadpool(firestore_service.compare_and_set, incident, "escalated_at", None, now)
    await run_in_threadpool(_record, incident, "incidencia_escalada", tracking.SYSTEM_ACTOR, now, reason)

    configuration = TenantScopedQuery.get_one_for_business(Configurations, business.bussiness_id)
    names = {normalize_phone(c.get("phone")): c.get("name") for c in (configuration.couriers if configuration else None) or []}
    customer = Customers.collection.get(incident.customer_id)
    order = Orders.collection.get(incident.order_id)
    lines = [
        f"🚨 Novedad en la entrega del pedido #{incident.daily_code}",
        f"📝 {incident.motivo}",
        f"⚠️ {reason}",
        f"👤 {(customer.name if customer else None) or 'Cliente'} · 📱 {phone_for_humans(customer) if customer else local_phone(incident.customer_phone)}",
        f"📍 {(order.delivery_address if order else None) or 'sin dirección'}",
        f"🛵 {names.get(incident.courier_phone) or 'Domiciliario'} · {local_phone(incident.courier_phone)}",
        "Por favor llámalo.",
    ]
    await HandoffService().notify_admins(business, "\n".join(lines), ATENCION_CLIENTE)
    await send_to_customer(business, incident.courier_customer_id, f"Ya avisamos al administrador del pedido #{incident.daily_code}. Espera sus instrucciones.")
    return True


async def on_timeout(incident_id: str, attempt: int, now: Optional[datetime] = None) -> str:
    """Temporizador de Cloud Tasks. Devuelve qué hizo (para el log)."""
    now = now or datetime.now(timezone.utc)
    incident = Incidents.collection.get(incident_id)
    if incident is None or incident.status != IncidentStatus.ESPERANDO_CLIENTE.value or int(incident.attempts or 0) != attempt:
        return "sin_cambios"
    business = Businesses.collection.get(incident.business_id)
    configuration = TenantScopedQuery.get_one_for_business(Configurations, incident.business_id)
    wait, retries = _settings(configuration)

    if attempt <= retries:
        if not await run_in_threadpool(firestore_service.compare_and_set, incident, "attempts", attempt, attempt + 1):
            return "sin_cambios"  # otra corrida del mismo temporizador ya lo tomó
        incident.attempts = attempt + 1
        await run_in_threadpool(_record, incident, "incidencia_reintento", tracking.SYSTEM_ACTOR, now)
        await _tell_customer(business, incident, _retry_message(incident.daily_code))
        await run_in_threadpool(cloud_tasks_client.enqueue_incident_timeout, incident.incident_id, attempt + 1, wait * 60)
        return "reintento"

    escalated = await escalate(business, incident, "El cliente no responde.", now)
    return "escalada" if escalated else "sin_cambios"


async def escalate_without_order(business: Businesses, courier: dict, motivo: str) -> bool:
    """El domiciliario no logró dar un código de comanda válido: que lo
    atienda el admin directamente."""
    text = (
        f"🛵 {courier.get('name') or 'Un domiciliario'} ({local_phone(courier.get('phone'))}) reporta un problema "
        f"con una entrega, pero no dio un número de comanda válido.\n📝 {' '.join((motivo or '').split())[:300]}\n"
        "Por favor comunícate con él."
    )
    return await HandoffService().notify_admins(business, text, ATENCION_CLIENTE)


# --- Respuesta del cliente -----------------------------------------------------

# Lo que el cliente puede decidir. Las de _TO_ADMIN no las resuelve la IA.
CUSTOMER_ACTIONS = {
    "bajando": "el cliente ya sale a recibirlo",
    "porteria": "déjalo en la portería",
    "otra_direccion": "la entrega es en otro punto",
    "otro": "el cliente respondió",
    "otro_telefono": None,
    "cancelar": None,
    "cambio_pago": None,
}
_TO_ADMIN = {
    "otro_telefono": "El cliente pide que lo llamen a otro número",
    "cancelar": "El cliente quiere cancelar el pedido",
    "cambio_pago": "El cliente quiere cambiar el pago o el cobro",
}
# Un teléfono nunca viaja al domiciliario aunque el cliente lo escriba.
_PHONE_LIKE = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def find_active_for_customer(business_id: str, customer_id: Optional[str]) -> Optional[Incidents]:
    """La incidencia activa (esperando al cliente o escalada) de hoy del cliente."""
    if not customer_id:
        return None
    today = tracking.service_date(business_id)
    candidates = TenantScopedQuery.list_for_business(Incidents, business_id, customer_id=customer_id, service_date=today)
    active = [i for i in candidates if i.status in ACTIVE]
    return max(active, key=lambda i: i.created_at, default=None)


def customer_note(incident: Incidents) -> str:
    """Nota del sistema para el turno de la IA del cliente (no se guarda)."""
    return (
        "[Nota del sistema, no visible para el cliente: hay una novedad de entrega en su pedido "
        f"#{incident.daily_code}: el domiciliario está en su dirección y no ha podido entregarlo. "
        "Si lo que escribe responde a eso (ya sale, que lo dejen en portería, otra torre o "
        "apartamento, otro número, cancelar...), llama a responder_incidencia antes de contestarle.]\n"
    )


async def _set_status(incident: Incidents, new_status: str, now: datetime) -> bool:
    """Cambia el estado si nadie lo cambió antes (relee y compara)."""
    current = Incidents.collection.get(incident.incident_id)
    if current is None or current.status not in ACTIVE:
        return False
    if not await run_in_threadpool(firestore_service.compare_and_set, current, "status", current.status, new_status):
        return False
    incident.status = new_status
    field = "resolved_at" if new_status == IncidentStatus.RESUELTA.value else None
    if field:
        await run_in_threadpool(firestore_service.compare_and_set, current, field, None, now)
    return True


async def resolve_from_customer(
    business: Businesses, incident: Incidents, action: str, detail: str = "", now: Optional[datetime] = None
) -> dict:
    """Aplica la decisión del cliente. Returns {"ok", "resultado":
    "instruccion_enviada" | "escalado", ...} para que su IA le conteste."""
    now = now or datetime.now(timezone.utc)
    if action not in CUSTOMER_ACTIONS:
        return {"ok": False, "reason": "accion_invalida", "acciones": sorted(CUSTOMER_ACTIONS)}
    detail = " ".join((detail or "").split())[:200]
    was_escalated = incident.status == IncidentStatus.ESCALADA.value
    await run_in_threadpool(_record, incident, "respuesta_cliente", incident.customer_phone, now, f"{action}: {detail}".strip(": "))

    if action in _TO_ADMIN:
        reason = _TO_ADMIN[action] + (f": {detail}" if detail else ".")
        if not was_escalated:
            await escalate(business, incident, reason, now)
        else:
            await HandoffService().notify_admins(business, f"📦 Pedido #{incident.daily_code}: {reason}", ATENCION_CLIENTE)
        return {"ok": True, "resultado": "escalado", "indicacion": "Dile que el restaurante se comunica con él en un momento para eso."}

    # "Ya bajo" no agrega nada a "el cliente ya sale a recibirlo".
    safe_detail = "" if action == "bajando" else _PHONE_LIKE.sub("[número]", detail)
    instruction = f"📦 Pedido #{incident.daily_code}: {CUSTOMER_ACTIONS[action]}" + (f": {safe_detail}" if safe_detail else "") + "."
    await send_to_customer(business, incident.courier_customer_id, instruction)
    await _set_status(incident, IncidentStatus.RESUELTA.value, now)
    await run_in_threadpool(_record, incident, "incidencia_resuelta", incident.customer_phone, now, instruction)
    if was_escalated:
        # El admin ya estaba en esto: que sepa que el cliente sí respondió.
        await HandoffService().notify_admins(
            business, f"✅ El cliente del pedido #{incident.daily_code} respondió: {CUSTOMER_ACTIONS[action]}" + (f" ({safe_detail})" if safe_detail else "") + ".", ATENCION_CLIENTE
        )
    return {"ok": True, "resultado": "instruccion_enviada", "indicacion": "Dile que ya le avisaste al domiciliario."}


async def escalate_busy_customer(business: Businesses, incident: Incidents, now: Optional[datetime] = None) -> bool:
    """El chat del cliente está con una persona (handoff): el bot no le
    contesta, así que la novedad la resuelve esa persona."""
    return await escalate(business, incident, "El cliente está en un chat con el equipo: resuélvanlo por ahí.", now)
