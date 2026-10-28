"""Reglas de negocio de cada tenant que usa la IA (tono, formas de pago,
pedido mínimo, preguntas frecuentes), leídas de Configurations.

Caché en memoria del proceso con TTL corto: la instrucción del agente se
arma en cada llamada al modelo y no debe leer Firestore cada vez. Editar la
configuración por la API admin (que vive en este mismo worker) la invalida.
"""

import time
from datetime import datetime, timezone
from typing import Optional

from app.firestore_db.firebase_schemas import Businesses, Configurations
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services import opening_hours
from app.services.daily_menu.menu_diff import format_cop

_TTL_SECONDS = 60
DEFAULT_PAYMENT_METHODS = ["efectivo", "transferencia"]

# shortcut: caché por proceso; con más de una instancia del worker, un
# cambio tarda hasta _TTL_SECONDS en verse en las demás.
_cache: dict[str, tuple[float, dict]] = {}


def get_profile(business_id: str) -> dict:
    cached = _cache.get(business_id)
    if cached and time.monotonic() - cached[0] < _TTL_SECONDS:
        return cached[1]
    configuration = TenantScopedQuery.get_one_for_business(Configurations, business_id)
    business = Businesses.collection.get(business_id) if business_id else None
    profile = {
        "nombre": (configuration.restaurant_name if configuration else None) or (business.name if business else None),
        "tono": configuration.tono if configuration else None,
        "metodos_pago": (configuration.metodos_pago if configuration else None) or DEFAULT_PAYMENT_METHODS,
        "pedido_minimo": configuration.pedido_minimo if configuration else None,
        "cuentas_transferencia": (configuration.cuentas_transferencia if configuration else None) or [],
        "faq": (configuration.faq if configuration else None) or [],
        "opening_hours": (configuration.opening_hours if configuration else None) or {},
    }
    _cache[business_id] = (time.monotonic(), profile)
    return profile


def invalidate(business_id: str) -> None:
    _cache.pop(business_id, None)


def render_profile(business_id: Optional[str]) -> str:
    """Bloque "Negocio" de la instrucción del agente de clientes."""
    if not business_id:
        return ""
    profile = get_profile(business_id)
    lines = [f"Negocio: {profile['nombre']}"] if profile["nombre"] else []
    if profile["tono"]:
        lines.append(f"Tono con los clientes: {profile['tono']}")
    lines.append("Formas de pago aceptadas: " + ", ".join(profile["metodos_pago"]))
    if profile["pedido_minimo"]:
        lines.append(f"Pedido mínimo (sin domicilio): {format_cop(profile['pedido_minimo'], '')}")
    if profile["faq"]:
        lines.append("Preguntas frecuentes (responde con esto, sin inventar):")
        lines += [f"- {f.get('pregunta')}: {f.get('respuesta')}" for f in profile["faq"]]
    lines += _hours_lines(business_id, profile["opening_hours"])
    return "\n".join(lines)


def _hours_lines(business_id: str, hours: dict) -> list[str]:
    state = opening_hours.status(business_id, hours)
    if not state["configured"]:
        return []
    lines = [f"Horario de atención: {opening_hours.describe_weekly(hours)}"]
    if state["open"]:
        lines.append("Ahora: ABIERTO.")
        return lines
    now = datetime.now(timezone.utc)
    lines.append(f"Ahora: CERRADO. Próxima apertura: {opening_hours.describe_next_opening(state['next_opening'], now)}.")
    lines.append(
        "No se pueden confirmar pedidos hasta entonces; sí puedes resolver dudas."
        if state["enforce"]
        else "Si el cliente pide, avísale que el pedido se atiende cuando abran."
    )
    return lines
