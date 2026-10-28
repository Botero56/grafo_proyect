"""Planes comerciales (Businesses.plan) y qué funciones incluye cada uno.

- básico: bot de clientes, IA del admin y domicilios por zona. Las
  transferencias las valida un humano (validar_transferencia).
- avanzado: + lectura automática del comprobante y la impresora.
- premium: + domiciliarios (bot, liquidación, incidencias, cierre diario) y
  reportes (estadísticas, consultas de pedidos, Excel).

Los pedidos se guardan en todos los planes: lo que se limita es el acceso.
Un negocio sin plan (los creados antes de esto) cuenta como premium, para
no quitarle nada a quien ya lo usa.

El bloqueo vive en el backend, no solo en el panel: cada punto de entrada
pregunta `allows()` (las tools del agente, el comprobante, el rol del
domiciliario, la impresión y el cierre diario).
"""

from enum import Enum
from typing import Optional, Union

from app.firestore_db.firebase_schemas import Businesses


class Plan(str, Enum):
    BASICO = "basico"
    AVANZADO = "avanzado"
    PREMIUM = "premium"


COMPROBANTE_AUTO = "comprobante_auto"
IMPRESORA = "impresora"
DOMICILIARIOS = "domiciliarios"
REPORTES = "reportes"

FEATURES: dict[Plan, frozenset[str]] = {
    Plan.BASICO: frozenset(),
    Plan.AVANZADO: frozenset({COMPROBANTE_AUTO, IMPRESORA}),
    Plan.PREMIUM: frozenset({COMPROBANTE_AUTO, IMPRESORA, DOMICILIARIOS, REPORTES}),
}

# Tools de los agentes que solo corren si el plan incluye la función.
TOOL_FEATURES = {
    "list_couriers": DOMICILIARIOS,
    "add_courier": DOMICILIARIOS,
    "remove_courier": DOMICILIARIOS,
    "liquidacion_domiciliarios": DOMICILIARIOS,
    "responder_incidencia": DOMICILIARIOS,
    "query_orders_nl": REPORTES,
    "generate_excel_report": REPORTES,
}


def plan_of(business: Optional[Businesses]) -> Plan:
    value = getattr(business, "plan", None)
    return Plan(value) if value in Plan._value2member_map_ else Plan.PREMIUM


def features_of(business: Optional[Businesses]) -> frozenset[str]:
    return FEATURES[plan_of(business)]


def allows(business: Union[Businesses, str, None], feature: str) -> bool:
    """¿El plan del negocio incluye `feature`? Acepta el negocio o su id."""
    if isinstance(business, str):
        business = Businesses.collection.get(business)
    return feature in features_of(business)


def required_plan(feature: str) -> Plan:
    """El plan más barato que incluye `feature`."""
    return next(plan for plan in Plan if feature in FEATURES[plan])


def gate_tools(tool, args, tool_context) -> Optional[dict]:
    """before_tool_callback de ADK: si el plan no incluye la tool, no corre
    y el modelo recibe por qué (y le explica al usuario)."""
    feature = TOOL_FEATURES.get(tool.name)
    if feature is None or allows(tool_context.state.get("business_id"), feature):
        return None
    return {
        "ok": False,
        "reason": "no_incluido_en_el_plan",
        "plan_requerido": required_plan(feature).value,
    }
