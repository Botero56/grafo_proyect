"""Seguimiento de entregas y liquidación de domiciliarios.

Dos cosas separadas a propósito:
- Estado de entrega (operativo): se infiere por tiempo (`sweep`) y por lo que
  dice el cliente (`report_customer_update`). Nunca atribuye domiciliario.
- Atribución (contable): sale de la liquidación que hace cada domiciliario
  (`settle`, también para "entregué 18").

Todo vive en el documento de referencia del pedido (el primer ítem, el que
lleva order_total). El consecutivo del día y el índice código -> pedido
viven en DailyOrderCounters, uno por negocio y día. Cada cambio deja un
evento en la subcolección status_history del pedido.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.firestore_db.firebase_schemas import Businesses, Configurations, Customers, DailyOrderCounters, Incidents, Orders, OrderStatusHistory
from app.schemas.enum_schemas import DeliveryEventSource, IncidentStatus, OrderStatus, PaymentMethod, PaymentStatus
from app.services import opening_hours, plans, tenant_profile
from app.services.conversation.handoff_service import (
    ATENCION_CLIENTE,
    CIERRE_DIARIO,
    HandoffService,
    local_phone,
    phone_for_humans,
)
from app.services.daily_menu.menu_diff import format_cop
from app.services.firestore_services import firestore_service
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services.phones import normalize_phone
from app.services.time_utils import as_utc

logger = logging.getLogger("whatsapp-webhook")

SYSTEM_ACTOR = "sistema"
# Incidencias de entrega abiertas (ver delivery/incidents.py): las cierra la liquidación.
ACTIVE_INCIDENTS = (IncidentStatus.ESPERANDO_CLIENTE.value, IncidentStatus.ESCALADA.value)
# Pago aún sin resolver: la comanda no ha salido a cocina, no se presume entrega.
_HELD_PAYMENTS = {
    PaymentStatus.PENDIENTE_COMPROBANTE.value,
    PaymentStatus.PENDIENTE_VALIDACION.value,
    PaymentStatus.PENDIENTE_AUTORIZACION.value,
}
CUSTOMER_SIGNALS = {"ya_llego", "comprobante", "queja_contenido"}
INQUIRY = "pregunta_por_pedido"


def _tz(business_id: str) -> ZoneInfo:
    hours = tenant_profile.get_profile(business_id)["opening_hours"]
    return ZoneInfo(hours.get("timezone") or opening_hours.DEFAULT_TZ)


def service_date(business_id: str, now: Optional[datetime] = None) -> str:
    """Día del negocio 'YYYY-MM-DD' en su zona horaria."""
    return (now or datetime.now(timezone.utc)).astimezone(_tz(business_id)).date().isoformat()


def _counter_id(business_id: str, day: str) -> str:
    return f"{business_id}_{day}"


def _event(order: Orders, event_type: str, source: str, actor: str, new_status: str, now: datetime, notes: Optional[str] = None) -> OrderStatusHistory:
    return OrderStatusHistory(
        parent=order.key,
        order_id=order.order_id,
        business_id=order.business_id,
        event_type=event_type,
        previous_status=order.status,
        new_status=new_status,
        source=source,
        actor=actor,
        notes=notes,
        created_at=now,
    )


def assign_daily_code(order: Orders, now: Optional[datetime] = None) -> int:
    """Le da al pedido el siguiente consecutivo del día de su negocio. Atómico
    entre procesos; si el pedido ya tiene código (reintento), devuelve ese."""
    now = now or datetime.now(timezone.utc)
    day = service_date(order.business_id, now)
    counter_id = _counter_id(order.business_id, day)

    def _assign(t):
        counter = DailyOrderCounters.collection.get(counter_id, transaction=t)
        fresh = Orders.collection.get(order.order_id, transaction=t)
        if fresh.daily_code:
            return int(fresh.daily_code)
        if counter is None:
            counter = DailyOrderCounters(counter_id=counter_id, business_id=order.business_id, date=day, last=0, orders={})
        code = int(counter.last or 0) + 1
        counter.last = code
        counter.orders = {**(counter.orders or {}), str(code): order.order_id}
        fresh.daily_code, fresh.service_date = code, day
        counter.save(transaction=t)
        fresh.save(transaction=t)
        return code

    code = firestore_service.run_in_transaction(_assign)
    order.daily_code, order.service_date = code, day
    return code


def _orders_of_day(business_id: str, day: str) -> dict[int, Orders]:
    counter = DailyOrderCounters.collection.get(_counter_id(business_id, day))
    orders = {}
    for code, order_id in ((counter.orders if counter else None) or {}).items():
        order = Orders.collection.get(order_id)
        if order is not None and order.business_id == business_id:
            orders[int(code)] = order
    return orders


def order_by_code(business_id: str, code: int, now: Optional[datetime] = None) -> Optional[Orders]:
    """La comanda #code de hoy del negocio, o None."""
    counter = DailyOrderCounters.collection.get(_counter_id(business_id, service_date(business_id, now)))
    order_id = ((counter.orders if counter else None) or {}).get(str(code))
    order = Orders.collection.get(order_id) if order_id else None
    return order if order is not None and order.business_id == business_id else None


def is_own_order(order: Orders, courier_phone: str, transaction=None) -> bool:
    """True si el pedido lo hizo el propio domiciliario como cliente."""
    customer = Customers.collection.get(order.customer_id, transaction=transaction) if order.customer_id else None
    return customer is not None and normalize_phone(customer.phone_number) == normalize_phone(courier_phone)


def settle(business_id: str, courier_phone: str, codes: list[int], source: str, now: Optional[datetime] = None) -> dict:
    """Atribuye a `courier_phone` las comandas de hoy con esos códigos y las
    marca entregado_confirmado, en una transacción (dos domiciliarios a la
    vez no se pisan). Idempotente: repetir el mismo mensaje no cambia nada.

    Returns: {"ok": [códigos atribuidos ahora], "mine": [ya eran suyos],
    "unknown": [no existen hoy], "own": [pedidos que hizo él mismo como
    cliente: nunca se los liquida], "conflicts": [{"code", "other", "new"}]}.
    `new` = el conflicto se registró con este mensaje (para avisar una vez).
    """
    now = now or datetime.now(timezone.utc)
    courier_phone = normalize_phone(courier_phone)
    day = service_date(business_id, now)
    counter_id = _counter_id(business_id, day)
    codes = list(dict.fromkeys(codes))

    def _settle(t):
        counter = DailyOrderCounters.collection.get(counter_id, transaction=t)
        index = (counter.orders if counter else None) or {}
        orders = {c: Orders.collection.get(index[str(c)], transaction=t) for c in codes if str(c) in index}
        # Todas las lecturas antes de la primera escritura.
        own = {c for c, o in orders.items() if o is not None and is_own_order(o, courier_phone, t)}
        open_incidents = {
            c: incident
            for c, o in orders.items()
            if o is not None and o.active_incident_id
            and (incident := Incidents.collection.get(o.active_incident_id, transaction=t)) is not None
            and incident.status in ACTIVE_INCIDENTS
        }
        result = {"ok": [], "mine": [], "unknown": [], "own": [], "conflicts": []}
        for code in codes:
            order = orders.get(code)
            if order is None or order.business_id != business_id:
                result["unknown"].append(code)
            elif code in own:
                result["own"].append(code)
            elif order.courier_phone == courier_phone:
                result["mine"].append(code)
            elif order.courier_phone:
                new = courier_phone not in (order.conflict_courier_phones or [])
                result["conflicts"].append({"code": code, "other": order.courier_phone, "new": new})
                if new:
                    _event(order, "conflicto", source, courier_phone, order.status, now).save(transaction=t)
                    order.conflict_courier_phones = [*(order.conflict_courier_phones or []), courier_phone]
                    order.save(transaction=t)
            else:
                _event(order, "liquidado", source, courier_phone, OrderStatus.ENTREGADO_CONFIRMADO.value, now).save(transaction=t)
                order.status = OrderStatus.ENTREGADO_CONFIRMADO.value
                order.courier_phone = courier_phone
                order.delivered_at = now
                order.save(transaction=t)
                result["ok"].append(code)
            if code in open_incidents and code in result["ok"] + result["mine"]:
                # Liquidarla es la prueba de que se entregó: la novedad queda
                # resuelta sin que el admin tenga que cerrarla.
                incident = open_incidents[code]
                incident.status, incident.resolved_at, incident.updated_at = IncidentStatus.RESUELTA.value, now, now
                incident.save(transaction=t)
                _event(order, "incidencia_resuelta", source, courier_phone, order.status, now, "Resuelta al liquidar la comanda").save(transaction=t)
        return result

    return firestore_service.run_in_transaction(_settle)


def split_payment(order: Orders) -> tuple[float, float]:
    """(efectivo, transferencia) que se cobró por este pedido."""
    total = order.order_total or 0
    if order.payment_method == PaymentMethod.EFECTIVO.value:
        return total, 0
    if order.payment_method == PaymentMethod.TRANSFERENCIA.value:
        cash = order.cash_due or 0  # pago mixto: el resto en efectivo al recibir
        return cash, total - cash
    return 0, 0  # acuerdo con la administración: no se cobra


def courier_summary(business_id: str, courier_phone: str, now: Optional[datetime] = None) -> dict:
    """Todo lo liquidado hoy por el domiciliario (acumula varios mensajes)."""
    orders = TenantScopedQuery.list_for_business(
        Orders, business_id, service_date=service_date(business_id, now), courier_phone=normalize_phone(courier_phone)
    )
    cash = transfer = 0
    for order in orders:
        c, t = split_payment(order)
        cash, transfer = cash + c, transfer + t
    return {"count": len(orders), "codes": sorted(int(o.daily_code) for o in orders), "cash": cash, "transfer": transfer}


def _courier_lines(orders: dict[int, Orders], couriers: list, show_codes: bool) -> list[str]:
    """Una línea por domiciliario con lo que liquidó: comandas, efectivo y
    transferencia que debe tener."""
    names = {normalize_phone(c.get("phone")): c.get("name") for c in couriers or []}
    by_courier: dict[str, list[int]] = {}
    for code, order in orders.items():
        if order.status == OrderStatus.ENTREGADO_CONFIRMADO.value and order.courier_phone:
            by_courier.setdefault(order.courier_phone, []).append(code)
    lines = []
    for phone, codes in sorted(by_courier.items(), key=lambda kv: names.get(kv[0]) or kv[0]):
        cash = sum(split_payment(orders[c])[0] for c in codes)
        transfer = sum(split_payment(orders[c])[1] for c in codes)
        listed = " (" + ", ".join(f"#{c}" for c in sorted(codes)) + ")" if show_codes else ""
        lines.append(
            f"• {names.get(phone) or local_phone(phone)}: {len(codes)} comanda(s){listed} · "
            f"Efectivo {format_cop(cash, '-')} · Transferencia {format_cop(transfer, '-')}"
        )
    return lines


def couriers_report(business_id: str, day: str, couriers: list) -> str:
    """Qué entregó (liquidó) cada domiciliario ese día, lo que falta por
    liquidar y las novedades de entrega. Para el chat de administración:
    las cuentas salen de acá, no del modelo."""
    orders = _orders_of_day(business_id, day)
    if not orders:
        return f"🛵 {day}: no hay comandas ese día."
    lines = [f"🛵 Domicilios {day} ({len(orders)} comandas)"]
    lines += _courier_lines(orders, couriers, show_codes=True) or ["• Ningún domiciliario ha liquidado todavía."]
    pending = sorted(c for c, o in orders.items() if o.status != OrderStatus.ENTREGADO_CONFIRMADO.value)
    if pending:
        lines.append("Sin liquidar: " + ", ".join(f"#{c}" for c in pending))
    day_incidents = TenantScopedQuery.list_for_business(Incidents, business_id, service_date=day)
    if day_incidents:
        lines.append("Novedades: " + ", ".join(f"#{i.daily_code} {i.status}" for i in sorted(day_incidents, key=lambda i: i.daily_code or 0)))
    return "\n".join(lines)


def _transition(order_id: str, event_type: str, source: str, actor: str, now: datetime, new_status: Optional[str] = None, only_from: tuple = (), notes: Optional[str] = None) -> Optional[Orders]:
    """Registra un evento y, si el pedido está en uno de `only_from`, lo pasa
    a `new_status`. En transacción: no pisa una liquidación simultánea."""

    def _run(t):
        order = Orders.collection.get(order_id, transaction=t)
        if order is None:
            return None
        target = new_status if new_status and order.status in only_from else order.status
        _event(order, event_type, source, actor, target, now, notes).save(transaction=t)
        if target != order.status:
            order.status = target
            order.save(transaction=t)
        return order

    return firestore_service.run_in_transaction(_run)


def find_open_order(business_id: str, customer_id: str, now: Optional[datetime] = None) -> Optional[Orders]:
    """El pedido de hoy más reciente del cliente (documento de referencia)."""
    orders = TenantScopedQuery.list_for_business(
        Orders, business_id, customer_id=customer_id, service_date=service_date(business_id, now)
    )
    return max(orders, key=lambda o: as_utc(o.created_at), default=None)


def _elapsed(order: Orders, now: datetime) -> str:
    minutes = int((now - as_utc(order.confirmed_at or order.created_at)).total_seconds() // 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"


async def report_customer_update(business: Businesses, customer: Customers, kind: str, customer_text: str, now: Optional[datetime] = None) -> dict:
    """Lo que el agente de clientes detectó en el turno (sin otra llamada al
    modelo). `pregunta_por_pedido` pasa el pedido a con_reclamo y avisa ya a
    la administradora; las señales (ya_llego, comprobante, queja_contenido)
    solo quedan como evento: nunca atribuyen domiciliario."""
    now = now or datetime.now(timezone.utc)
    if kind != INQUIRY and kind not in CUSTOMER_SIGNALS:
        return {"ok": False, "reason": "Tipo no válido."}
    order = find_open_order(business.bussiness_id, customer.customer_id, now)
    if order is None:
        return {"ok": False, "reason": "El cliente no tiene pedidos hoy."}
    actor = normalize_phone(customer.phone_number)
    text = " ".join((customer_text or "").split())[:300]
    if kind != INQUIRY:
        _transition(order.order_id, kind, DeliveryEventSource.SENAL_CLIENTE.value, actor, now, notes=text)
        return {"ok": True}

    _transition(
        order.order_id, "reclamo", DeliveryEventSource.RECLAMO_CLIENTE.value, actor, now,
        new_status=OrderStatus.CON_RECLAMO.value,
        only_from=(OrderStatus.EN_COCINA.value, OrderStatus.PRESUNTO_ENTREGADO.value),
        notes=text,
    )
    lines = [
        f"🛵 Cliente pregunta por su pedido #{order.daily_code}",
        f"📍 {order.delivery_address or 'sin dirección'}",
        f"⏱️ Hace {_elapsed(order, now)}",
        f"💬 \"{text}\"",
        f"📱 {phone_for_humans(customer)}",
        "↩️ Respóndeme cuánto falta (ej. \"20 min\") y yo le aviso.",
    ]
    notified = await HandoffService().notify_admins(business, "\n".join(lines), ATENCION_CLIENTE)
    return {"ok": True, "admin_notified": notified}


def _cut_passed(cut: Optional[str], day: str, tz: ZoneInfo, now: datetime) -> bool:
    if not cut:
        return False
    hour, minute = (int(x) for x in cut.split(":"))
    cut_at = datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=tz)
    return now >= cut_at


def _closing_message(day: str, orders: dict[int, Orders], couriers: list) -> str:
    names = {normalize_phone(c.get("phone")): c.get("name") for c in couriers or []}
    pending = sorted(c for c, o in orders.items() if o.status != OrderStatus.ENTREGADO_CONFIRMADO.value)
    cash_codes = [c for c in pending if split_payment(orders[c])[0]]
    missing_cash = sum(split_payment(orders[c])[0] for c in pending)
    lines = [
        f"📋 Cierre de domicilios {day}",
        f"Comandas del día: {len(orders)}",
        f"Liquidadas: {len(orders) - len(pending)}",
        *_courier_lines(orders, couriers, show_codes=False),
    ]
    if pending:
        listed = ", ".join(f"#{c}" + (" (efectivo)" if c in cash_codes else "") for c in pending)
        lines.append(f"No liquidadas ({len(pending)}): {listed}")
        lines.append(f"Efectivo sin liquidar: {format_cop(missing_cash, '-')}")
    conflicts = [(c, o) for c, o in sorted(orders.items()) if o.conflict_courier_phones]
    for code, order in conflicts:
        who = [order.courier_phone, *order.conflict_courier_phones]
        lines.append(f"⚠️ #{code} liquidado por varios: " + ", ".join(names.get(p) or local_phone(p) for p in who))
    return "\n".join(lines)


async def close_day(business: Businesses, configuration: Configurations, day: str, now: datetime) -> bool:
    """Pasa a no_liquidado lo que nadie liquidó y le manda UN mensaje a la
    administradora. Una sola vez por día (DailyOrderCounters.closed_at)."""
    business_id = business.bussiness_id
    counter = DailyOrderCounters.collection.get(_counter_id(business_id, day))
    if counter is None or counter.closed_at is not None:
        return False
    orders = _orders_of_day(business_id, day)
    open_states = (OrderStatus.EN_COCINA.value, OrderStatus.PRESUNTO_ENTREGADO.value, OrderStatus.CON_RECLAMO.value)
    for order in orders.values():
        if order.status in open_states:
            updated = _transition(
                order.order_id, "no_liquidado", DeliveryEventSource.CIERRE.value, SYSTEM_ACTOR, now,
                new_status=OrderStatus.NO_LIQUIDADO.value, only_from=open_states,
            )
            if updated is not None:
                order.status = updated.status
    # Marca antes de enviar: dos corridas del job no mandan dos cierres.
    if not firestore_service.compare_and_set(counter, "closed_at", None, now):
        return False
    await HandoffService().notify_admins(business, _closing_message(day, orders, configuration.couriers), CIERRE_DIARIO)
    return True


async def sweep(now: Optional[datetime] = None) -> dict:
    """Job de Cloud Scheduler (cada 10 min): presume entregados los pedidos
    que pasaron el tiempo estimado sin reclamo y hace el cierre diario de los
    negocios que ya pasaron su hora de corte (también el de ayer, si el job
    no alcanzó a correr antes de medianoche)."""
    now = now or datetime.now(timezone.utc)
    presumed, closed = 0, []
    for business in Businesses.collection.filter(status="active").fetch():
        business_id = business.bussiness_id
        configuration = TenantScopedQuery.get_one_for_business(Configurations, business_id)
        if configuration is None:
            continue
        try:
            tz = _tz(business_id)
            today = service_date(business_id, now)
            yesterday = (datetime.fromisoformat(today) - timedelta(days=1)).date().isoformat()
            minutes = configuration.tiempo_entrega_estimado_min
            # shortcut: lee los pedidos del día de cada negocio en cada corrida; si crece mucho, filtrar por status.
            if minutes:
                for order in _orders_of_day(business_id, today).values():
                    due = as_utc(order.confirmed_at or order.created_at) + timedelta(minutes=minutes)
                    if order.status == OrderStatus.EN_COCINA.value and order.payment_status not in _HELD_PAYMENTS and now >= due:
                        _transition(
                            order.order_id, "presunto_entregado", DeliveryEventSource.TIEMPO_ESTIMADO.value, SYSTEM_ACTOR, now,
                            new_status=OrderStatus.PRESUNTO_ENTREGADO.value, only_from=(OrderStatus.EN_COCINA.value,),
                        )
                        presumed += 1
            cut = configuration.hora_cierre_domicilios
            # El cierre es la liquidación de los domiciliarios: solo en su plan.
            for day in (yesterday, today) if plans.allows(business, plans.DOMICILIARIOS) else ():
                if (day == yesterday and cut) or _cut_passed(cut, day, tz, now):
                    if await close_day(business, configuration, day, now):
                        closed.append({"business_id": business_id, "date": day})
        except Exception:
            # Un negocio con datos raros no frena el job de los demás.
            logger.exception("Falló el barrido de domicilios del negocio %s", business_id)
    return {"presumed": presumed, "closed": closed}
