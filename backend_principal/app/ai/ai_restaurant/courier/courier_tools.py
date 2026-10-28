"""Tools del agente de domiciliarios.

Quien escribe ya fue verificado como domiciliario por el backend (ver
role_resolver): las tools leen business_id y customer_id del state de la
sesión y comprueban que su teléfono siga en Configurations.couriers. Las
cuentas de la liquidación las hace el código (couriers.settle_and_report),
nunca el modelo.
"""

from typing import Optional

from google.adk.tools import ToolContext
from starlette.concurrency import run_in_threadpool

from app.firestore_db.firebase_schemas import Businesses, Configurations, Customers
from app.schemas.enum_schemas import DeliveryEventSource
from app.services.delivery import couriers, incidents, tracking
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient


def _courier_context(tool_context: ToolContext) -> Optional[tuple[str, Configurations, Customers, dict]]:
    business_id = tool_context.state.get("business_id")
    customer = Customers.collection.get(tool_context.state.get("customer_id"))
    configuration = TenantScopedQuery.get_one_for_business(Configurations, business_id)
    courier = couriers.find_courier(configuration, customer.phone_number) if customer else None
    if courier is None:
        return None
    return business_id, configuration, customer, courier


async def liquidar_comandas(tool_context: ToolContext, codigos: list[int], base: Optional[int] = None) -> dict:
    """Liquida las comandas que el domiciliario entregó hoy, a su nombre.

    Úsala cuando diga que entregó o quiere liquidar comandas y ya tengas los
    números (ej. "ya dejé la 12 y la 15", "liquídame el 3"). Si no dio los
    números, pregúntaselos; nunca los adivines.

    Args:
        codigos: números de comanda (los cortos del día, ej. [12, 15]).
        base: dinero de base (para cambio) que dice que lleva, en pesos (ej.
            "base de 75 mil" -> 75000). Omítelo si no lo mencionó.

    Returns:
        {"ok": True, "texto": ...}: muéstrale `texto` tal cual (trae lo
        registrado, lo que no existe, conflictos y su acumulado del día).
        {"ok": False, "reason": "no_es_domiciliario"} si ya no está registrado.
    """
    context = await run_in_threadpool(_courier_context, tool_context)
    if context is None:
        return {"ok": False, "reason": "no_es_domiciliario"}
    business_id, configuration, _, courier = context
    business = await run_in_threadpool(Businesses.collection.get, business_id)
    text = await couriers.settle_and_report(
        WhatsAppClient.for_business(business), business_id, configuration, courier,
        [int(c) for c in codigos], DeliveryEventSource.LIQUIDACION.value, base,
    )
    return {"ok": True, "texto": text}


def mi_resumen_de_hoy(tool_context: ToolContext, base: Optional[int] = None) -> dict:
    """Lo que el domiciliario lleva liquidado hoy: comandas, efectivo y
    transferencia. Úsala cuando pregunte cuánto lleva, cuánto debe entregar
    o qué comandas tiene registradas.

    Args:
        base: dinero de base que dice que lleva, en pesos, si lo mencionó.

    Returns:
        {"ok": True, "texto": ...}: muéstrale `texto` tal cual.
    """
    context = _courier_context(tool_context)
    if context is None:
        return {"ok": False, "reason": "no_es_domiciliario"}
    business_id, _, customer, _ = context
    summary = tracking.courier_summary(business_id, customer.phone_number)
    return {"ok": True, "texto": couriers.format_summary(summary, base)}


def usar_modo_cliente(tool_context: ToolContext) -> dict:
    """Pasa al domiciliario a modo cliente hasta que confirme su pedido,
    para que pueda pedir comida como cualquier cliente. Úsala solo cuando
    quede claro que quiere hacer un pedido para él (ej. "quiero pedir un
    almuerzo").

    Returns:
        {"ok": True}: dile que ya puede escribir su pedido. Sus liquidaciones
        claras ("liquidar 12") se siguen registrando igual.
    """
    context = _courier_context(tool_context)
    if context is None:
        return {"ok": False, "reason": "no_es_domiciliario"}
    business_id, _, customer, _ = context
    customer.courier_as_customer_date = tracking.service_date(business_id)
    FireStoreCRUDService().update(customer)
    return {"ok": True}


_INVALID_KEY = "incidencia_codigos_invalidos"
_MAX_INVALID = 2
_REASONS = {
    "no_existe": "Esa comanda no existe hoy: pídele que revise el número.",
    "ya_entregada": "Esa comanda ya figura entregada o cerrada: pregúntale si es otro número.",
    "es_tu_pedido": "Esa comanda es un pedido suyo como cliente: no se reporta como entrega.",
}


async def reportar_incidencia(tool_context: ToolContext, codigo: int, motivo: str) -> dict:
    """Reporta un problema con una entrega (no abren, la portería no
    responde, la dirección está mal, el cliente no contesta...). El sistema
    le escribe al cliente de esa comanda y, si no responde a tiempo, avisa
    al administrador.

    Úsala cuando el domiciliario cuente un problema en una entrega y ya
    tengas el número de comanda. Si no lo dio, pregúntaselo.

    Args:
        codigo: número de la comanda (el corto del día, ej. 12).
        motivo: el problema en una frase, con sus palabras (ej. "no abren y
            la portería no contesta").

    Returns:
        {"ok": True, "texto": ...}: muéstrale `texto` tal cual.
        {"ok": False, "reason": ..., "indicacion": ...}: sigue la indicación.
        Con "escalado_al_admin" ya se le avisó al administrador: díselo.
    """
    context = await run_in_threadpool(_courier_context, tool_context)
    if context is None:
        return {"ok": False, "reason": "no_es_domiciliario"}
    business_id, _, customer, courier = context
    business = await run_in_threadpool(Businesses.collection.get, business_id)
    result = await incidents.open_incident(business, courier, customer.customer_id, int(codigo), motivo)
    if result["ok"]:
        tool_context.state[_INVALID_KEY] = 0
        return result
    if result["reason"] != "no_existe":
        return {**result, "indicacion": _REASONS[result["reason"]]}
    # Máximo 2 códigos inválidos: después lo atiende el admin directamente.
    invalid = int(tool_context.state.get(_INVALID_KEY) or 0) + 1
    if invalid >= _MAX_INVALID:
        tool_context.state[_INVALID_KEY] = 0
        await incidents.escalate_without_order(business, courier, motivo)
        return {"ok": False, "reason": "escalado_al_admin"}
    tool_context.state[_INVALID_KEY] = invalid
    return {**result, "indicacion": _REASONS["no_existe"]}
