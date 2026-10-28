"""
Tools usadas por el admin_agent: consultas en lenguaje natural y reportes.

El business_id nunca lo decide el modelo: ya lo resolvió el webhook y viaja
en el state de la sesión (mismo patrón que app.ai.ai_restaurant.user.tools
.customer_tools), por eso se lee de tool_context en vez de pedirlo como
argumento.

Ninguna tool tiene acceso directo al WhatsAppClient a propósito, para no
mezclar "calcular datos" con "hablar con la API de Meta" y para poder
testear estas funciones sin credenciales de WhatsApp. Cuando
generate_excel_report genera un archivo, lo anuncia dejando un
"pending_document" en el state de la sesión; el webhook (después de recibir
la respuesta de texto del agente) es quien revisa ese flag y hace el envío
real con WhatsAppClient.send_document.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from google.adk.tools import ToolContext
from pydantic import ValidationError

from app.ai.core.agent_runner import shared_agent_runner
from app.firestore_db.firebase_schemas import Businesses, Configurations, Customers, Orders
from app.schemas.enum_schemas import OrderStatus
from app.schemas.api.configuration_schemas import DeliveryZone, check_unique_zones
from app.services import opening_hours
from app.services.conversation.handoff_service import HandoffService
from app.services.daily_menu import catalog_orders, catalog_sync, stock
from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia
from app.services.daily_menu.daily_menu_service import DailyMenuService
from app.services.daily_menu.menu_formatter import format_confirmation_message
from app.services.delivery import incidents, quotes, tracking
from app.services.payments import payment_agreement, transfer_receipt
from app.services.phones import is_username_id, normalize_phone
from app.services.delivery.zones import normalize
from app.services.excel_report_service.constants import DEFAULT_TZ, PRODUCT_ITEM_LABELS
from app.services.excel_report_service.dashboard_report import compute_dashboard_summary
from app.services.excel_report_service.excel_report_service import ExcelReportService
from app.services.excel_report_service.firestore_repository import (
    FirestoreReportRepository,
)
from app.services.excel_report_service.models import ExcelReportSpec
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.firestore_webhook_service import find_active_conversation
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient
from app.ai.ai_restaurant.user.tools.customer_tools import active_menu

_repo = FirestoreReportRepository()

_WEEKDAYS_ES = [
    "lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo",
]


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    """Convierte 'YYYY-MM-DD' a datetime con la zona horaria del negocio.

    Mismo criterio que firestore_repository.as_datetime: una fecha sin
    zona horaria se interpreta directamente en DEFAULT_TZ, no se convierte.
    """
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=DEFAULT_TZ)


def get_current_datetime() -> dict:
    """Devuelve la fecha/hora actual del negocio.

    El admin_agent debe llamar esta tool ANTES de resolver fechas relativas
    ("hoy", "ayer", "esta semana", "este mes"): el modelo no conoce la
    fecha real de cuando ocurre la conversación, solo la de su
    entrenamiento, así que nunca debe asumirla.

    Returns:
        dict, ej. {"date": "2026-09-12", "weekday": "sábado",
                   "now": "2026-09-12T10:32:00-05:00"}
    """
    now = datetime.now(DEFAULT_TZ)
    return {
        "now": now.isoformat(),
        "date": now.date().isoformat(),
        "weekday": _WEEKDAYS_ES[now.weekday()],
    }


def get_today_menu(tool_context: ToolContext) -> dict:
    """Devuelve el menú del día vigente HOY, ya formateado en texto listo
    para mostrárselo al admin en el chat -- el mismo formato que ve al
    cargar/confirmar la foto (menú del día, otras opciones, acompañamientos,
    envíos/contacto).

    Usa ESTA tool cuando el admin pregunte cuál es el menú de hoy, si ya lo
    guardó, o pida verlo/confirmarlo por texto. NUNCA uses
    generate_excel_report para esto -- no existe un reporte en Excel del
    menú del día, solo esta tool (que además es más rápida y más clara
    para una simple consulta de texto).

    Returns:
        dict, ej. {"menu_available": True, "closed_today": False,
            "closed_reason": None, "text": "*Menú del lunes..."}
        menu_available: hay menú del día hoy. carta_available: hay carta
        (productos fijos). Pueden estar los dos: el text trae el menú del
        día y la carta juntos, tal como los ven hoy los clientes --
        muéstraselo tal cual. carta_tipo ("catalogo" | "foto" | None) y
        catalog_link dicen qué se le manda al cliente de la carta.
        Si no hay ninguno de los dos, text es None.
        Si el admin marcó el negocio como cerrado hoy (ver
        mark_closed_today):
            {"menu_available": False, "closed_today": True,
             "closed_reason": "..." | None, "text": None}
    """
    business_id = tool_context.state.get("business_id")
    today = resolve_fecha_vigencia(None)
    service = DailyMenuService()

    closed = service.get_closed_for_date(business_id, today)
    if closed is not None:
        return {"menu_available": False, "closed_today": True, "closed_reason": closed.caption, "text": None}

    texts = service.today_menu_texts(business_id, today)
    active_carta = service.get_active_carta(business_id)
    carta = (active_carta.extracted_menu or {}) if active_carta is not None else {}
    return {
        "menu_available": service.get_active_for_date(business_id, today) is not None,
        "carta_available": bool(service.active_fixed_items(business_id)),
        "closed_today": False,
        "closed_reason": None,
        "text": "\n\n".join(texts) or None,
        "carta_tipo": carta.get("tipo"),
        "catalog_link": carta.get("link"),
    }


def mark_closed_today(tool_context: ToolContext, motivo: Optional[str] = None) -> dict:
    """Guarda que el negocio NO abre hoy, para que la IA de clientes le
    avise a quien pregunte por el menú en vez de responder que "todavía no
    está listo" o pedir un humano por falta de datos.

    Úsala cuando el admin diga explícitamente que hoy no abren, no hay
    servicio, están cerrados, de vacaciones, etc. (ej. "hoy no abrimos",
    "no va a haber menú", "cerramos por remodelación") -- típicamente en
    respuesta al recordatorio de que aún no ha llegado la foto del menú,
    pero funciona en cualquier momento del día.

    Si más tarde el admin sí manda la foto del menú de hoy, la confirmación
    normal reemplaza este aviso (no hace falta deshacerlo a mano).

    Args:
        motivo: la razón que dio el admin, tal cual (ej. "cerrado por
            remodelación"), o None si no dio ninguna -- se le muestra al
            cliente si existe, si no solo se le avisa que no abren hoy.

    Returns:
        dict, ej. {"closed": True, "date": "2026-09-22", "motivo": "..."}
    """
    business_id = tool_context.state.get("business_id")
    today = resolve_fecha_vigencia(None)

    DailyMenuService().mark_closed_today(business_id, today, _admin_phone(tool_context), motivo)
    opening_hours.invalidate(business_id)
    return {"closed": True, "date": today, "motivo": motivo}


def reuse_last_menu(tool_context: ToolContext) -> dict:
    """Deja como menú de HOY el último menú que el admin confirmó, sin
    mandar foto. Úsala cuando diga "usa el de ayer", "el mismo de ayer",
    "repite el menú". Si hoy estaba marcado como cerrado, deja de estarlo.

    Returns:
        {"ok": True, "date": "2026-10-17", "text": "*Menú del ..."} o
        {"ok": False, "reason": "ya_hay_menu_hoy" | "sin_menu_anterior"}
    """
    business_id = tool_context.state.get("business_id")
    today = resolve_fecha_vigencia(None)

    menu, reason = DailyMenuService().reuse_last_menu(business_id, today, _admin_phone(tool_context))
    if menu is None:
        return {"ok": False, "reason": reason}
    messages = format_confirmation_message(DailyMenuService.get_extracted_menu(menu), today, changes=None)
    return {"ok": True, "date": today, "text": "\n\n".join(messages)}


async def request_stock_change(
    tool_context: ToolContext, producto: str, cantidad: Optional[int] = None, agotado: bool = False
) -> dict:
    """Pide confirmar un cambio de stock de HOY. NO lo guarda: le manda al
    admin un mensaje con botones Sí/No y el cambio se aplica solo si toca
    "Sí".

    Úsala cuando el admin diga que algo se acabó o cuánto queda, ej. "se
    agotó el pollo", "ya no hay limonada", "quedan 5 hamburguesas", "solo me
    quedan dos de ajiaco", o que algo volvió ("ya llegó más pollo, sin
    límite").

    Args:
        producto: el producto tal como lo dijo el admin (ej. "pollo").
        cantidad: cuántos quedan, como número entero (ej. "cinco" -> 5).
            Omítelo si se agotó o si vuelve a estar sin límite.
        agotado: True si dijo que se acabó / ya no hay.
        (Sin cantidad y con agotado=False = vuelve a estar sin límite.)

    Returns:
        {"ok": True, "producto": "Pollo"}: se le mandó la confirmación; dile
            en una frase que la confirme con el botón (todavía NO quedó).
        {"ok": False, "reason": "ambiguo", "opciones": [...]}: pregúntale
            cuál de esas es.
        {"ok": False, "reason": "no_encontrado", "productos_hoy": [...]}:
            no hay un producto así hoy; muéstrale cuáles hay.
        {"ok": False, "reason": "cantidad_invalida"}.
    """
    if cantidad is not None and (isinstance(cantidad, bool) or cantidad < 0 or cantidad != int(cantidad)):
        return {"ok": False, "reason": "cantidad_invalida"}
    business_id = tool_context.state.get("business_id")
    products = stock.menu_products(active_menu(business_id))
    matches = stock.match_product(producto, products)
    if len(matches) > 1:
        return {"ok": False, "reason": "ambiguo", "opciones": matches}
    if not matches:
        return {"ok": False, "reason": "no_encontrado", "productos_hoy": sorted(set(products.values()))}

    nombre = matches[0]
    body, buttons = stock.confirmation(nombre, 0 if agotado else (None if cantidad is None else int(cantidad)))
    business = Businesses.collection.get(business_id)
    await WhatsAppClient.for_business(business).send_interactive_buttons(
        _admin_phone(tool_context), body, buttons
    )
    return {"ok": True, "producto": nombre}


def _admin_phone(tool_context: ToolContext) -> Optional[str]:
    customer_id = tool_context.state.get("customer_id")
    admin = Customers.collection.get(customer_id) if customer_id else None
    return admin.phone_number if admin else None


def query_orders_nl(
    tool_context: ToolContext,
    question: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """Responde preguntas del administrador sobre pedidos/ventas.

    No genera ningún archivo (para eso está generate_excel_report): trae
    los pedidos del rango pedido -o de todo el histórico si no se da
    rango- y calcula un resumen en Python. Usar esto para preguntas
    puntuales tipo "¿cuánto vendimos ayer?" o "¿cuántos cancelados hubo
    esta semana?".

    Args:
        question: la pregunta del administrador, tal cual la escribió
            (solo se usa para trazabilidad/logs, no cambia el cálculo).
        date_from: fecha inicial en formato ISO (YYYY-MM-DD), opcional.
        date_to: fecha final en formato ISO (YYYY-MM-DD, exclusiva),
            opcional. Si el admin pregunta por un solo día, usar ese día
            como date_from y el día siguiente como date_to.

    Returns:
        dict con el resumen calculado, ej.
            {"total_orders": 42, "cancelled_orders": 3,
             "delivered_orders": 39, "total_revenue": 1250000.0,
             "top_items": [{"menu_id": "menu_dia", "quantity": 20,
                             "revenue": 300000.0}, ...]}
    """
    business_id = tool_context.state.get("business_id")

    spec = ExcelReportSpec(
        business_id=business_id,
        report_type="orders",
        date_from=_parse_date(date_from),
        date_to=_parse_date(date_to),
    )
    orders = _repo.fetch_orders(spec)

    cancelled = [o for o in orders if o.get("status") == "cancelled"]
    delivered = [o for o in orders if o.get("status") != "cancelled"]
    total_revenue = sum(float(o.get("total") or 0) for o in delivered)

    items: dict[str, dict] = {}
    for order in delivered:
        key = order.get("menu_id") or "sin_producto"
        entry = items.setdefault(
            key, {"menu_id": key, "item_name": order.get("item_name"), "quantity": 0, "revenue": 0.0}
        )
        entry["quantity"] += int(order.get("quantity") or 0)
        entry["revenue"] += float(order.get("total") or 0)

    # menu_id ahora es literalmente "menu_del_dia" u "otras_opciones" (ver
    # save_order en customer_tools.py; "plato_del_dia" es el item_id viejo
    # de pedidos hechos antes del rename) -- se resuelve a un nombre
    # legible con la misma lista estática que usan los reportes de ventas,
    # en vez de cruzar contra Firestore.
    menu_names = {item["item_id"]: item.get("name") for item in PRODUCT_ITEM_LABELS}
    for key, entry in items.items():
        entry["name"] = menu_names.get(key) or entry.pop("item_name") or key

    top_items = sorted(items.values(), key=lambda item: item["quantity"], reverse=True)[:5]

    return {
        "question": question,
        "date_from": date_from,
        "date_to": date_to,
        "total_orders": len(orders),
        "cancelled_orders": len(cancelled),
        "delivered_orders": len(delivered),
        "total_revenue": total_revenue,
        "top_items": top_items,
    }


def _find_customer(business_id: str, cliente: str) -> tuple[Optional[dict], dict]:
    """(cliente, {}) si queda claro quién es; (None, {}) si no existe;
    (None, {"reason": "varios_clientes", "opciones": [...]}) si el nombre
    corresponde a varios: la IA le pregunta al admin cuál."""
    found = _repo.find_customers(business_id, cliente)
    if len(found) == 1:
        return found[0], {}
    if not found:
        return None, {}
    options = [{"nombre": c.get("name"), "telefono": c.get("phone_number")} for c in found[:10]]
    return None, {"reason": "varios_clientes", "opciones": options}


def find_orders_by_customer(
    tool_context: ToolContext,
    cliente: str,
) -> dict:
    """Busca un cliente por su teléfono o su nombre y devuelve su número y sus pedidos.

    Usar esto cuando el admin pregunte por un cliente puntual (ej. "¿qué
    pidió el 3001234567?", "muéstrame los pedidos de ese número", "¿qué
    comprobante mandó?"), en vez de query_orders_nl (que agrega todo el
    negocio, no filtra por cliente).

    Si el admin pregunta por el comprobante de una transferencia, dale el
    enlace de `transfer_receipt_urls` (el último es el más reciente; se
    abre con la cuenta de Google del negocio).

    LIMITACIÓN: customer_id en Orders solo existe para pedidos guardados
    después de que se conectó este campo -- pedidos viejos (incluyendo datos
    de prueba cargados antes) no van a aparecer acá aunque sean del mismo
    cliente. Si el resultado viene vacío pero el admin insiste en que ese
    cliente sí pidió algo, puede ser por eso, no necesariamente porque no
    exista el cliente.

    Args:
        cliente: el cliente como lo nombró el admin: su teléfono (ej.
            "3001234567") o su nombre o parte de él (ej. "Camilo"). Con un
            nombre, búscalo aquí: no respondas de memoria.

    Returns:
        dict, ej. {"found_customer": True, "customer_name": "...",
            "orders": [{"order_id": ..., "menu_id": ..., "item_name": ...,
            "quantity": ..., "total": ..., "status": ..., "created_at": ...,
            "payment_method": ..., "payment_status": ..., "payment_note": ...,
            "transfer_amount":
            ..., "tip_amount": ..., "transfer_receipt_urls": [...]}, ...]}
            Los datos de pago y comprobantes van solo en el primer ítem de
            cada pedido (el que lleva order_total).
        Si no existe ningún cliente con ese teléfono en este negocio,
        {"found_customer": False, "orders": []}.
    """
    business_id = tool_context.state.get("business_id")

    customer, problem = _find_customer(business_id, cliente)
    if customer is None:
        return {"found_customer": False, "orders": [], **problem}

    orders = _repo.fetch_orders_by_customer(business_id, customer["customer_id"])

    menu_names = {item["item_id"]: item.get("name") for item in PRODUCT_ITEM_LABELS}

    return {
        "found_customer": True,
        "customer_name": customer.get("name"),
        "phone_number": customer.get("phone_number"),
        "orders": [
            {
                "order_id": order.get("order_id"),
                "menu_id": order.get("menu_id"),
                "item_name": menu_names.get(order.get("menu_id")) or order.get("item_name") or order.get("menu_id"),
                "quantity": order.get("quantity"),
                "total": order.get("total"),
                "status": order.get("status"),
                "created_at": order.get("created_at").isoformat()
                if order.get("created_at")
                else None,
                "payment_method": order.get("payment_method"),
                "payment_status": order.get("payment_status"),
                "payment_note": order.get("payment_note"),
                "transfer_amount": order.get("transfer_amount"),
                "tip_amount": order.get("tip_amount"),
                "transfer_receipt_urls": order.get("transfer_receipt_urls") or [],
            }
            for order in orders
        ],
    }


def get_customer_chat_reference(tool_context: ToolContext, cliente: str) -> dict:
    """Devuelve el teléfono y el enlace directo de WhatsApp de un cliente
    puntual, para que el admin abra y lea esa conversación él mismo.

    Úsala SIEMPRE que el admin pida información, resumen o detalles sobre
    LA CONVERSACIÓN de un cliente en específico (ej. "¿de qué hablé con
    Camilo?", "muéstrame el chat del 3001234567", "resume esa
    conversación"). Tienes PROHIBIDO sugerir o generar un Excel para esto,
    aunque el admin lo pida -- no hay reporte de texto/transcripción de
    chats, y no existe forma de resumir una conversación completa sin
    inventar contenido. En vez de eso, esta tool te da la referencia para
    que el admin vaya directo a leerla él mismo en la app de WhatsApp.

    (Si en cambio pregunta por los PEDIDOS de un cliente, no su
    conversación, usa find_orders_by_customer.)

    Args:
        cliente: el cliente como lo nombró el admin: su teléfono (ej.
            "3001234567") o su nombre o parte de él (ej. "Camilo"). Con un
            nombre, búscalo aquí: no respondas de memoria.

    Returns:
        dict, ej. {"found": True, "customer_name": "Camilo",
            "phone_number": "573001234567",
            "whatsapp_link": "https://wa.me/573001234567"}
        Si no existe ningún cliente con ese teléfono en este negocio:
            {"found": False}
    """
    business_id = tool_context.state.get("business_id")

    customer, problem = _find_customer(business_id, cliente)
    if customer is None:
        return {"found": False, **problem}

    whatsapp_id = customer.get("phone_number")
    phone = customer.get("contact_phone") or (None if is_username_id(whatsapp_id) else whatsapp_id)
    if phone is None:
        return {
            "found": True,
            "customer_name": customer.get("name"),
            "whatsapp_link": None,
            "nota": "Escribe con usuario de WhatsApp y no ha dado su número: no hay link directo, que lo busque por su nombre en WhatsApp Business.",
        }
    return {
        "found": True,
        "customer_name": customer.get("name"),
        "phone_number": phone,
        "whatsapp_link": f"https://wa.me/{phone.lstrip('+')}",
    }


def reactivate_customer_chat(tool_context: ToolContext, cliente: str) -> dict:
    """Reactiva el bot para un cliente puntual después de que la encargada
    terminó de atenderlo a mano.

    Hoy la única forma automática de reactivar es por inactividad (el job
    programado, que tarda horas) o por un endpoint admin aparte -- esta
    tool le da a la encargada una tercera vía, directo desde el chat: usar
    esto cuando el admin diga algo como "ya atendí al 3001234567, que siga
    el bot", "reactiva a ese cliente" o "termine con esa conversación,
    puede seguir la IA".

    Args:
        cliente: el cliente como lo nombró el admin: su teléfono (ej.
            "3001234567") o su nombre o parte de él (ej. "Camilo"). Con un
            nombre, búscalo aquí: no respondas de memoria.

    Returns:
        dict, ej. {"reactivated": True, "customer_name": "..."}.
        Si no existe ese cliente en este negocio:
            {"reactivated": False, "reason": "cliente_no_encontrado"}
        Si el cliente existe pero no tenía el bot pausado (nada que hacer):
            {"reactivated": False, "reason": "no_estaba_pausado"}
    """
    business_id = tool_context.state.get("business_id")

    customer, problem = _find_customer(business_id, cliente)
    if customer is None:
        return {"reactivated": False, "reason": "cliente_no_encontrado", **problem}

    conversation = find_active_conversation(business_id, customer["customer_id"])
    if conversation is None or conversation.bot_activo is not False:
        return {"reactivated": False, "reason": "no_estaba_pausado"}

    HandoffService().reactivate_bot(conversation)

    return {"reactivated": True, "customer_name": customer.get("name")}


async def set_delivery_quote(tool_context: ToolContext, cliente: str, fee: float) -> dict:
    """Registra el valor del domicilio que cotizó el admin para un cliente
    cuya dirección está fuera de las zonas, y se lo comunica al cliente.

    Úsala cuando el admin responda al aviso de "cotizar domicilio", ej.
    "domicilio 6000 para Camilo", "cóbrale 5.000 a 3001234567" o solo
    "6000" si hay un único cliente esperando.

    Args:
        cliente: a quién, como lo dijo el admin: su nombre (o parte), su
            teléfono o su código de usuario de WhatsApp. "" si no lo dijo.
        fee: valor del domicilio en pesos, como número (ej. 6000).

    Returns:
        {"ok": True, "customer_name": ..., "address": ..., "delivery_fee":
        ..., "customer_notified": bool}; {"ok": False, "reason":
        "sin_cotizacion_pendiente"} si nadie espera cotización; o {"ok":
        False, "reason": "cliente_no_identificado", "pendientes": [...]}:
        pregúntale al admin cuál de esos es, por nombre.
    """
    business_id = tool_context.state.get("business_id")

    pending = quotes.pending_quotes(business_id)
    if not pending:
        return {"ok": False, "reason": "sin_cotizacion_pendiente"}
    found = quotes.match_pending(pending, cliente)
    if len(found) != 1:
        return {
            "ok": False,
            "reason": "cliente_no_identificado",
            "pendientes": [{"nombre": c.name, "direccion": conv.delivery_address} for c, conv in pending],
        }
    customer, conversation = found[0]

    business = Businesses.collection.get(business_id)
    notified = await quotes.set_quote(business, customer, conversation, fee)
    return {
        "ok": True,
        "customer_name": customer.name,
        "address": conversation.delivery_address,
        "delivery_fee": fee,
        "customer_notified": notified,
    }


async def avisar_tiempo_entrega(tool_context: ToolContext, minutos: int, cliente: str = "") -> dict:
    """Le avisa a un cliente cuánto falta, más o menos, para que le llegue
    su pedido de hoy. El mensaje lo arma y lo envía el sistema.

    Úsala cuando el admin responda a un aviso de "Cliente pregunta por su
    pedido" (o pida avisarle a un cliente) con un tiempo, ej. "dile que en
    20 minutos", "a Camilo que llega en media hora" (30) o solo "15 min" si
    hay un único cliente preguntando. Si el admin no dio un tiempo,
    pregúntaselo: nunca lo inventes.

    Args:
        minutos: minutos aproximados, como número (media hora = 30, una hora = 60).
        cliente: como lo nombró el admin: nombre (o parte) o teléfono. "" si no lo dijo.

    Returns:
        {"ok": True, "customer_name": ..., "pedido": N, "customer_notified": bool};
        {"ok": False, "reason": "cliente_no_identificado", "preguntando": [...]}:
        pregúntale al admin cuál, por nombre; {"ok": False, "reason":
        "sin_pedido_hoy"} o "minutos_invalidos".
    """
    business_id = tool_context.state.get("business_id")
    try:
        minutos = int(minutos)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "minutos_invalidos"}
    if not 1 <= minutos <= 240:
        return {"ok": False, "reason": "minutos_invalidos"}

    if (cliente or "").strip():
        found, problem = _find_customer(business_id, cliente)
        if found is None:
            return {"ok": False, "reason": problem.get("reason", "cliente_no_encontrado"), **problem}
        customer_id = found["customer_id"]
    else:
        # Sin nombre: el único cliente que está preguntando por su pedido hoy.
        asking = {
            o.customer_id
            for o in TenantScopedQuery.list_for_business(
                Orders, business_id, status=OrderStatus.CON_RECLAMO.value, service_date=tracking.service_date(business_id)
            )
        }
        if len(asking) != 1:
            names = [getattr(Customers.collection.get(c), "name", None) for c in asking]
            return {"ok": False, "reason": "cliente_no_identificado", "preguntando": names}
        customer_id = asking.pop()

    order = tracking.find_open_order(business_id, customer_id)
    if order is None:
        return {"ok": False, "reason": "sin_pedido_hoy"}
    business = Businesses.collection.get(business_id)
    text = f"⏱️ Tu pedido #{order.daily_code} llega en aproximadamente {minutos} minutos. ¡Gracias por esperarnos!"
    sent = await incidents.send_to_customer(business, customer_id, text)
    conversation = find_active_conversation(business_id, customer_id)
    if conversation is not None:
        # Su IA no ve lo que se le manda por fuera: si vuelve a preguntar, que sepa qué se le dijo.
        await shared_agent_runner().append_human_message(
            business_id=business_id, conversation_id=conversation.conversation_id,
            user_id=customer_id, text=text, by_system=True,
        )
    customer = Customers.collection.get(customer_id)
    return {"ok": True, "customer_name": getattr(customer, "name", None), "pedido": order.daily_code, "customer_notified": sent}


async def resolve_payment_agreement(tool_context: ToolContext, cliente: str, cobrar: bool) -> dict:
    """Registra si se le cobra o no el pedido a un cliente que dijo tener un
    acuerdo especial de pago (tiquetera, pago mensual, ya pagado...), y se
    lo comunica al cliente.

    Úsala cuando el admin responda al aviso de "autorizar pago", ej. "no
    cobrar 573001234567", "ese cliente tiene tiquetera, no le cobres" o "sí,
    cóbrale a 573001234567". Si no queda claro si se cobra o no, o falta el
    teléfono, pregúntale al admin antes de llamarla.

    Args:
        cliente: el cliente como lo nombró el admin: su teléfono (ej.
            "3001234567") o su nombre o parte de él (ej. "Camilo"). Con un
            nombre, búscalo aquí: no respondas de memoria.
        cobrar: False si el pedido NO se cobra (sale a cocina marcado "no
            cobrar"); True si sí se cobra (al cliente se le pide efectivo o
            transferencia como siempre).

    Returns:
        {"ok": True, "customer_name": ..., "cobrar": bool,
        "customer_notified": bool}, o {"ok": False, "reason": ...} si no
        existe el cliente o no tiene un pedido esperando esta respuesta.
    """
    business_id = tool_context.state.get("business_id")

    customer_data, problem = _find_customer(business_id, cliente)
    if customer_data is None:
        return {"ok": False, "reason": "cliente_no_encontrado", **problem}
    order = payment_agreement.find_order_awaiting_authorization(business_id, customer_data["customer_id"])
    if order is None:
        return {"ok": False, "reason": "sin_pedido_esperando_autorizacion"}

    business = Businesses.collection.get(business_id)
    customer = Customers.collection.get(customer_data["customer_id"])
    notified = await payment_agreement.resolve(business, customer, order, charge=cobrar)
    return {"ok": True, "customer_name": customer.name, "cobrar": cobrar, "customer_notified": notified}


async def validar_transferencia(tool_context: ToolContext, cliente: str, aprobado: bool) -> dict:
    """Registra si llegó o no la transferencia de un cliente, y se lo
    comunica al cliente.

    Úsala cuando el admin responda un aviso de "transferencia por validar" o
    "pago por validar", o diga que ya le llegó (o no) la plata de un cliente,
    ej. "pago ok 573001234567", "sí llegó la de Camilo", "pago rechazado
    3001234567", "no me ha llegado nada de Ana". Si no queda claro si llegó
    o falta el cliente, pregúntale al admin antes de llamarla.

    Args:
        cliente: el cliente como lo nombró el admin: su teléfono (ej.
            "3001234567") o su nombre o parte de él (ej. "Camilo"). Con un
            nombre, búscalo aquí: no respondas de memoria.
        aprobado: True si la plata llegó (el pedido pasa a preparación);
            False si no llegó (se le pide al cliente revisar o pagar en
            efectivo).

    Returns:
        {"ok": True, "customer_name": ..., "aprobado": bool,
        "customer_notified": bool}, o {"ok": False, "reason": ...} si no
        existe el cliente o no tiene una transferencia por validar.
    """
    business_id = tool_context.state.get("business_id")

    customer_data, problem = _find_customer(business_id, cliente)
    if customer_data is None:
        return {"ok": False, "reason": "cliente_no_encontrado", **problem}
    order = transfer_receipt.find_order_awaiting_validation(business_id, customer_data["customer_id"])
    if order is None:
        return {"ok": False, "reason": "sin_transferencia_por_validar"}

    business = Businesses.collection.get(business_id)
    customer = Customers.collection.get(customer_data["customer_id"])
    notified = await transfer_receipt.resolve_manually(business, customer, order, approved=aprobado)
    return {"ok": True, "customer_name": customer.name, "aprobado": aprobado, "customer_notified": notified}


def add_delivery_zone(
    tool_context: ToolContext, nombre: str, tarifa: float, alias: Optional[list[str]] = None
) -> dict:
    """Agrega una zona de domicilio con tarifa fija, o actualiza la tarifa y
    los alias de una que ya existe con ese nombre. Desde ese momento las
    direcciones de esa zona se cobran solas, sin pedir cotización.

    Úsala solo cuando el admin lo pida explícitamente (ej. "agrega la zona
    Vereda El Salado con domicilio de 6000", "sube Cerro Azul a 3500") y
    después de que confirme zona y valor con un sí explícito: cambia lo que
    se les cobra a todos los clientes de esa zona.

    Args:
        nombre: nombre de la zona como la escriben los clientes.
        tarifa: valor del domicilio en pesos (ej. 6000).
        alias: otras formas en que los clientes nombran esa zona, si el
            admin las da (ej. ["el salado", "salado"]).

    Returns:
        {"ok": True, "zonas": [{"nombre": ..., "tarifa": ...}, ...]}
        {"ok": False, "reason": "zona_invalida", "detalle": "..."}: la tarifa
        no es un entero >= 0, un alias tiene menos de 4 letras o un nombre o
        alias ya es de otra zona. Explícale al admin el detalle y no la guardes.
    """
    business_id = tool_context.state.get("business_id")
    configuration = TenantScopedQuery.get_one_for_business(Configurations, business_id)
    if configuration is None:
        return {"ok": False, "reason": "configuracion_no_encontrada"}

    # Mismas reglas que el PATCH del panel (tarifa entera >= 0, alias de 4+
    # letras, nada repetido entre zonas): es lo que se le cobra al cliente.
    try:
        zone = DeliveryZone(nombre=nombre, tarifa=tarifa, alias=list(alias or [])).model_dump()
        zones = [
            z for z in (configuration.delivery_zones or [])
            if normalize(z.get("nombre", "")) != normalize(zone["nombre"])
        ]
        zones.append(zone)
        check_unique_zones([DeliveryZone(**z) for z in zones])
    except ValidationError as exc:
        return {"ok": False, "reason": "zona_invalida", "detalle": "; ".join(e["msg"] for e in exc.errors())}
    except ValueError as exc:
        return {"ok": False, "reason": "zona_invalida", "detalle": str(exc)}
    configuration.delivery_zones = zones
    FireStoreCRUDService().update(configuration)
    return {"ok": True, "zonas": [{"nombre": z["nombre"], "tarifa": z["tarifa"]} for z in zones]}


def _business_configuration(tool_context: ToolContext) -> Optional[Configurations]:
    return TenantScopedQuery.get_one_for_business(Configurations, tool_context.state.get("business_id"))


def _courier_view(couriers: list) -> list[dict]:
    return [{"nombre": c.get("name"), "telefono": c.get("phone")} for c in couriers]


def list_couriers(tool_context: ToolContext) -> dict:
    """Lista los domiciliarios registrados del negocio (nombre y teléfono).

    Úsala cuando el admin pida ver, listar o buscar sus domiciliarios (ej.
    "¿qué domiciliarios tengo?", "pásame el número de Carlos"), y antes de
    eliminar uno del que solo dio el nombre, para saber su teléfono.

    Returns:
        {"ok": True, "domiciliarios": [{"nombre": ..., "telefono": ...}]}
    """
    configuration = _business_configuration(tool_context)
    if configuration is None:
        return {"ok": False, "reason": "configuracion_no_encontrada"}
    return {"ok": True, "domiciliarios": _courier_view(configuration.couriers or [])}


def add_courier(tool_context: ToolContext, telefono: str, nombre: str) -> dict:
    """Registra un domiciliario del negocio, o le cambia el nombre si ese
    teléfono ya estaba registrado.

    Úsala solo cuando el admin lo pida (ej. "agrega a Carlos 3001112233
    como domiciliario") y después de que confirme nombre y teléfono con un
    sí explícito. Si falta el nombre o el teléfono, pregúntaselo.

    Args:
        telefono: celular del domiciliario como lo escriba el admin (ej.
            "300 111 2233" o "+57 3001112233").
        nombre: nombre del domiciliario.

    Returns:
        {"ok": True, "domiciliarios": [...]}, o {"ok": False, "reason":
        "telefono_invalido"} si el número no tiene al menos 10 dígitos.
    """
    phone = normalize_phone(telefono)
    if len(phone) < 10:
        return {"ok": False, "reason": "telefono_invalido"}
    configuration = _business_configuration(tool_context)
    if configuration is None:
        return {"ok": False, "reason": "configuracion_no_encontrada"}

    couriers = [c for c in (configuration.couriers or []) if c.get("phone") != phone]
    couriers.append({"phone": phone, "name": nombre.strip(), "added_at": datetime.now(DEFAULT_TZ).isoformat()})
    configuration.couriers = couriers
    FireStoreCRUDService().update(configuration)
    return {"ok": True, "domiciliarios": _courier_view(couriers)}


def liquidacion_domiciliarios(tool_context: ToolContext, fecha: Optional[str] = None) -> dict:
    """Qué entregó cada domiciliario en un día: sus comandas liquidadas, el
    efectivo y la transferencia de cada uno, lo que falta por liquidar y las
    novedades de entrega.

    Úsala cuando el admin pregunte qué entregó o cuánto lleva un domiciliario
    (ej. "¿qué domicilios entregó John?", "¿cuánto debe entregar Carlos?",
    "¿cómo van los domiciliarios hoy?"). Si pregunta por uno solo, muéstrale
    su línea.

    Args:
        fecha: día "YYYY-MM-DD". Omítela para hoy; para "ayer" u otra fecha
            resuélvela primero con get_current_datetime.

    Returns:
        {"ok": True, "texto": ...}: muéstrale el texto (o la línea que pidió)
        sin recalcular montos. {"ok": False, "reason": "fecha_invalida"}.
    """
    business_id = tool_context.state.get("business_id")
    day = fecha or tracking.service_date(business_id)
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return {"ok": False, "reason": "fecha_invalida"}
    configuration = _business_configuration(tool_context)
    couriers = (configuration.couriers if configuration else None) or []
    return {"ok": True, "texto": tracking.couriers_report(business_id, day, couriers)}


def remove_courier(tool_context: ToolContext, telefono: str) -> dict:
    """Elimina un domiciliario del negocio por su teléfono.

    Úsala solo cuando el admin lo pida y después de que confirme a quién
    elimina con un sí explícito. Si el admin dio solo el nombre, busca el
    teléfono con list_couriers; si hay dos con ese nombre, pregúntale cuál.

    Args:
        telefono: celular del domiciliario (ej. "3001112233").

    Returns:
        {"ok": True, "eliminado": "Carlos", "domiciliarios": [...]}, o
        {"ok": False, "reason": "domiciliario_no_encontrado"}.
    """
    phone = normalize_phone(telefono)
    configuration = _business_configuration(tool_context)
    if configuration is None:
        return {"ok": False, "reason": "configuracion_no_encontrada"}

    couriers = configuration.couriers or []
    removed = next((c for c in couriers if c.get("phone") == phone), None)
    if removed is None:
        return {"ok": False, "reason": "domiciliario_no_encontrado"}
    configuration.couriers = [c for c in couriers if c is not removed]
    FireStoreCRUDService().update(configuration)
    return {"ok": True, "eliminado": removed.get("name"), "domiciliarios": _courier_view(configuration.couriers)}


async def sync_catalog(tool_context: ToolContext) -> dict:
    """Actualiza el menú con el catálogo de WhatsApp Business del negocio
    (productos, precios y disponibilidad) y dice qué cambió.

    Úsala cuando el admin avise que modificó el catálogo, ej. "actualicé el
    catálogo", "ya cambié los precios en el catálogo", "agregué productos
    al catálogo", "sincroniza el catálogo".

    Muéstrale al admin el resumen tal cual: qué productos se crearon, se
    modificaron (con el cambio) y se eliminaron. Si las tres listas vienen
    vacías, dile que no se encontró ningún cambio en el catálogo -- puede
    que lo haya editado en otro catálogo o que aún no se haya guardado.

    Returns:
        {"ok": True, "creados": ["Limonada", ...],
         "modificados": ["Bandeja paisa: precio $25.000 → $27.000", ...],
         "eliminados": ["Sopa del día", ...]}
        {"ok": False, "reason": "catalogo_no_configurado"} si el negocio no
        tiene catálogo conectado (díselo, lo configura el equipo técnico).
        {"ok": False, "reason": "error_leyendo_catalogo"} si Meta no
        respondió: no se cambió nada, que lo intente más tarde.
    """
    business_id = tool_context.state.get("business_id")
    configuration = TenantScopedQuery.get_one_for_business(Configurations, business_id)
    catalog_id = configuration.whatsapp_catalog_id if configuration else None
    if not catalog_id:
        return {"ok": False, "reason": "catalogo_no_configurado"}

    result = await catalog_sync.sync_catalog(Businesses.collection.get(business_id), catalog_id)
    if result is None:
        return {"ok": False, "reason": "error_leyendo_catalogo"}
    return {"ok": True, **result}


_NOT_ON_MENU = {"no_es_del_menu", "no es del menu", "ninguno", "no"}


async def resolve_catalog_code(tool_context: ToolContext, producto: str, codigo: str = "") -> dict:
    """Le dice al sistema qué producto del menú es un código del catálogo de
    WhatsApp que llegó en un carrito y no se reconoció, y completa los
    pedidos de los clientes que lo esperaban.

    Úsala cuando el admin responda al aviso "🛒 Llegó un pedido del catálogo
    con un producto que no reconozco", ej. "el mks25c5cms es la hamburguesa
    tradicional", "es el menú del día", "eso no es del menú".

    Args:
        producto: el producto como lo dijo el admin (nombre o parte), o
            "no_es_del_menu" si dijo que no es nada del menú.
        codigo: el código del aviso si lo mencionó; "" si no (sirve cuando
            hay un solo código pendiente).

    Returns:
        {"ok": True, "codigo": ..., "producto": "Hamburguesa" | "no es del
        menú", "clientes_avisados": n}: confírmale al admin; a los clientes
        ya se les escribió.
        {"ok": False, "reason": "sin_codigos_pendientes"}: no hay nada
        esperando.
        {"ok": False, "reason": "codigo_no_identificado", "pendientes":
        [{"codigo", "precio"}]}: pregúntale cuál de esos.
        {"ok": False, "reason": "producto_no_encontrado", "opciones":
        [...]}: muéstrale las opciones y pregúntale cuál es.
    """
    business_id = tool_context.state.get("business_id")
    pending = catalog_orders.pending_codes(business_id)
    if not pending:
        return {"ok": False, "reason": "sin_codigos_pendientes"}
    code = (codigo or "").strip()
    found = [m for m in pending if m.product_retailer_id == code] if code else pending
    if len(found) != 1:
        return {
            "ok": False,
            "reason": "codigo_no_identificado",
            "pendientes": [{"codigo": m.product_retailer_id, "precio": m.last_seen_price} for m in pending],
        }

    target, name = None, "no es del menú"
    if normalize(producto) not in _NOT_ON_MENU:
        match, options = catalog_orders.find_product(business_id, producto)
        if match is None:
            return {"ok": False, "reason": "producto_no_encontrado", "opciones": options}
        target, name = match

    notified = await catalog_orders.confirm_code(
        Businesses.collection.get(business_id), found[0], target, tool_context.state.get("customer_id")
    )
    return {"ok": True, "codigo": found[0].product_retailer_id, "producto": name, "clientes_avisados": notified}


def generate_excel_report(
    tool_context: ToolContext,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """Genera un reporte en Excel y lo deja listo para enviarse por WhatsApp.

    Hay un solo Excel: la plantilla del negocio (tablero de ventas) con los
    pedidos del rango pegados en su tabla de datos. Sirve para cualquier
    pedido de Excel/reporte del admin ("ventas de la semana", "el
    dashboard de septiembre", "el Excel del 1 al 15").

    El archivo se envía automáticamente al administrador justo después de
    esta respuesta (lo hace el webhook, no esta tool). No inventes ni
    ofrezcas un link: solo confirma que el reporte va en camino.

    FILTRO DE CALIDAD -- piénsalo ANTES de llamar a esta tool: el admin no
    es técnico, así que un Excel solo tiene sentido si va a traer
    información clara (nombres, teléfonos, montos, productos, fechas). Si
    lo que le vas a entregar serían básicamente IDs internos sin contexto,
    o no tienes claro que la información vaya a ser útil, NO llames a esta
    tool -- explícale en una frase simple por qué no generaste nada, sin
    inventar datos para rellenar el documento. Además, esta tool hace su
    propio chequeo de datos vacíos (ver abajo), como segunda red de
    seguridad, no como sustituto de tu propio criterio.

    NUNCA la uses para "¿de qué hablé con tal cliente?" ni para resumir la
    conversación de un cliente puntual -- para eso existe
    get_customer_chat_reference, que le da al admin el link directo para
    que lea el chat él mismo.

    No hay Excel de "el menú" -- para eso siempre usa get_today_menu,
    jamás esta tool.

    Args:
        date_from: fecha inicial en formato ISO (YYYY-MM-DD). Obligatoria.
        date_to: fecha final en formato ISO (YYYY-MM-DD, exclusiva: para
            "septiembre" es 2026-10-01). Obligatoria.

    Returns:
        Si hubo datos: {"status": "ok", "file_name": "...",
            "summary": {...}}. `summary` trae los mismos números/nombres
            del reporte en JSON liviano (totales, top ítems, desglose por
            estado, etc.) -- guardalo en tu contexto: si el admin pregunta
            algo puntual sobre ESTE reporte más adelante en la
            conversación (ej. "¿y cuál fue el ítem más vendido?"),
            responde desde `summary` directamente, sin volver a llamar a
            esta tool ni pedirle que abra el archivo.
        Si no había datos: {"status": "empty", "file_name": None,
            "message": "..."} -- no se generó ningún archivo; explícale al
            admin por qué con el texto de `message`, en lenguaje simple.
        Sin rango: {"status": "missing_dates", ...} -- resuelve las fechas
            (get_current_datetime si es relativa) y vuelve a llamar; si el
            admin no dio ningún período, preguntale.
    """
    business_id = tool_context.state.get("business_id")

    if not (date_from and date_to):
        return {
            "status": "missing_dates",
            "file_name": None,
            "message": "El Excel necesita date_from y date_to.",
        }

    spec = ExcelReportSpec(
        business_id=business_id,
        report_type="dashboard",
        date_from=_parse_date(date_from),
        date_to=_parse_date(date_to),
    )

    summary = compute_dashboard_summary(spec, _repo)
    if summary["row_count"] == 0:
        return {
            "status": "empty",
            "file_name": None,
            "message": "No hay pedidos en el rango pedido.",
        }

    path: Path = ExcelReportService().generate_report(spec)

    # El webhook revisa este flag en el state de la sesión después de recibir
    # la respuesta de texto del agente, y usa WhatsAppClient.send_document
    # para mandar el archivo. Ver app/services/messaging/inbound_pipeline.py.
    tool_context.state["pending_document"] = {
        "path": str(path),
        "filename": path.name,
    }

    return {"status": "ok", "file_name": path.name, "summary": summary}
