"""Pedidos que llegan del carrito del catálogo de WhatsApp (mensaje `order`).

Los atiende el código, nunca la IA. Cada código del carrito
(`product_retailer_id`) se traduce a un producto del menú, en este orden:
1. el `catalog_retailer_id` que deja la sincronización (catalog_sync);
2. un mapeo que confirmó el admin (CatalogMappings).
Kapso no deja leer el catálogo (404 "Unsupported endpoint"), así que en la
práctica casi todo pasa por el 2: lo que nadie ha identificado se le
pregunta al admin una sola vez por código, y el pedido del cliente espera
(confirm_order no deja confirmar) hasta que conteste con la tool
resolve_catalog_code.

Se cobra el precio del carrito (item_price × quantity): es el que vio el
cliente. Si difiere del menú se le avisa al admin; no se corrige.
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

from app.ai.ai_restaurant.user.tools import order_tools
from app.ai.core.agent_runner import shared_agent_runner
from app.firestore_db.firebase_schemas import (
    Businesses,
    CatalogMappings,
    Configurations,
    Conversations,
    Customers,
    FixedMenuItems,
    Messages,
)
from app.services.conversation.handoff_service import HandoffService, phone_for_humans
from app.services.daily_menu.menu_diff import format_cop
from app.services.delivery.zones import normalize
from app.services.firestore_services import firestore_service
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.firestore_webhook_service import CART_PLACEHOLDER
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient

logger = logging.getLogger("whatsapp-webhook")

PENDING, CONFIRMED, IGNORED = "pending", "confirmed", "ignored"
# Se pueden mapear, pero no se precargan: les falta sopa y proteína.
DAY_PRODUCTS = {"menu_del_dia": "Menú del día", "otras_opciones": "Otras opciones"}

CUSTOMER_WAIT = "¡Recibimos tu pedido! 🙌 Estamos confirmando un producto con el restaurante y ya te escribimos."
WRONG_CATALOG = "No pude leer tu carrito 🙏 ¿Me escribes qué productos quieres?"
UNAVAILABLE = "Uno de los productos de tu carrito no está disponible en este momento 🙏"


def mapping_id(business_id: str, catalog_id: str, retailer_id: str) -> str:
    return hashlib.sha256(f"{business_id}:{catalog_id}:{retailer_id}".encode()).hexdigest()[:32]


def resolve_items(business_id: str, catalog_id: str, product_items: list[dict]) -> tuple[list, list, list]:
    """(resueltos, desconocidos, ignorados). Cada uno lleva codigo, cantidad,
    precio y moneda del carrito; los resueltos además `producto` y
    `precio_menu` (None si no aplica)."""
    fixed = TenantScopedQuery.list_for_business(FixedMenuItems, business_id)
    by_code = {item.catalog_retailer_id: item for item in fixed if item.catalog_retailer_id}
    by_id = {item.fixed_item_id: item for item in fixed}

    resolved, unknown, ignored = [], [], []
    for raw in product_items:
        code = str(raw.get("product_retailer_id") or "")
        entry = {
            "codigo": code,
            "cantidad": raw.get("quantity") or 1,
            "precio": raw.get("item_price"),
            "moneda": raw.get("currency"),
        }
        item = by_code.get(code)
        if item is not None:
            resolved.append({**entry, "producto": f"fixed:{item.fixed_item_id}", "precio_menu": item.precio})
            continue
        mapping = TenantScopedQuery.get_by_id_for_business(
            CatalogMappings, business_id, mapping_id(business_id, catalog_id, code)
        )
        if mapping is not None and mapping.status == CONFIRMED:
            target = by_id.get((mapping.producto or "").removeprefix("fixed:"))
            resolved.append({**entry, "producto": mapping.producto, "precio_menu": target.precio if target else None})
        elif mapping is not None and mapping.status == IGNORED:
            ignored.append(entry)
        else:
            unknown.append(entry)
    return resolved, unknown, ignored


def register_unknown(business_id: str, catalog_id: str, entry: dict, conversation_id: str) -> bool:
    """Deja el código como `pending` y anota la conversación que lo espera.
    True solo para quien lo creó: así se le pregunta al admin una vez aunque
    lleguen dos carritos a la vez."""
    doc_id = mapping_id(business_id, catalog_id, entry["codigo"])

    def _run(t):
        doc = CatalogMappings.collection.get(doc_id, transaction=t)
        created = doc is None
        if created:
            doc = CatalogMappings(
                catalog_mapping_id=doc_id,
                business_id=business_id,
                catalog_id=catalog_id,
                product_retailer_id=entry["codigo"],
                status=PENDING,
                esperando=[],
                first_seen_at=datetime.now(timezone.utc),
            )
        # ponytail: si el admin lo resolvió justo entre resolve_items y acá,
        # este carrito queda esperando; el cliente vuelve a escribir.
        doc.esperando = list(dict.fromkeys([*(doc.esperando or []), conversation_id]))
        doc.last_seen_price = entry["precio"]
        doc.currency = entry["moneda"]
        doc.save(transaction=t)
        return created

    return firestore_service.run_in_transaction(_run)


def product_options(business_id: str) -> list[tuple[str, str, Optional[float]]]:
    """(producto, nombre, precio) de todo lo que puede ser un código: los
    productos fijos (aunque estén agotados hoy) y los del menú del día."""
    options = [
        (f"fixed:{item.fixed_item_id}", item.nombre, item.precio)
        for item in TenantScopedQuery.list_for_business(FixedMenuItems, business_id)
    ]
    return options + [(key, name, None) for key, name in DAY_PRODUCTS.items()]


def find_product(business_id: str, text: str) -> tuple[Optional[tuple[str, str]], list[str]]:
    """((producto, nombre), []) si `text` es un id o nombre que identifica
    un solo producto; (None, nombres posibles) si no."""
    options = product_options(business_id)
    key = normalize(text)
    exact = [o for o in options if key in (normalize(o[0]), normalize(o[1]))]
    partial = [o for o in options if key and key in normalize(o[1])]
    for found in (exact, partial):
        if len(found) == 1:
            return (found[0][0], found[0][1]), []
    return None, [o[1] for o in (partial or options)][:15]


def _candidates(business_id: str, price: Optional[float]) -> list[str]:
    priced = [(name, p) for _, name, p in product_options(business_id) if p is not None]
    priced.sort(key=lambda o: abs(o[1] - (price or 0)))
    return [f"{name} ({format_cop(p, '')})" for name, p in priced[:3]]


def pending_codes(business_id: str) -> list[CatalogMappings]:
    return TenantScopedQuery.list_for_business(CatalogMappings, business_id, status=PENDING)


def _summary(result: dict, ignored: list, note: str) -> str:
    """Lo que ve el agente en vez del carrito: nombres y precios, nunca códigos."""
    lines = [
        "[Pedido enviado desde el carrito del catálogo. El sistema ya lo cargó en el pedido: "
        "no vuelvas a agregar estos productos. Muéstrale el resumen y sigue con lo que falte.]"
    ]
    lines += [f"- {i['qty']}x {i['name']} {format_cop(i['subtotal'], '')}" for i in result["agregados"]]
    lines += [
        f"- {p['cantidad']}x {DAY_PRODUCTS[p['producto']]}: aún NO está en el pedido; pregúntale lo que falte y agrégalo con update_order"
        for p in result["por_elegir"]
    ]
    if result["errores"]:
        lines.append("No se pudo agregar: " + "; ".join(result["errores"]))
    if ignored:
        lines.append(f"{len(ignored)} producto(s) del carrito no están disponibles: díselo.")
    if note:
        lines.append(f'Nota del cliente en el carrito: "{note}"')
    return "\n".join(lines)


def _resolved_text(result: dict, ignored: bool) -> str:
    lines = []
    if result["agregados"] or result["por_elegir"]:
        lines.append("¡Listo! Ya confirmamos tu pedido 🙌")
        lines += [f"- {i['qty']}x {i['name']} {format_cop(i['subtotal'], '')}" for i in result["agregados"]]
        lines += [f"- {p['cantidad']}x {DAY_PRODUCTS[p['producto']]}" for p in result["por_elegir"]]
    if ignored or result["errores"]:
        lines.append(UNAVAILABLE)
    lines.append("Seguimos confirmando otro producto, ya te escribimos." if result["quedan"] else "¿Seguimos con tu pedido?")
    return "\n".join(lines)


async def _tell_customer(business: Businesses, customer: Customers, conversation_id: str, text: str) -> bool:
    sent = await WhatsAppClient.for_business(business).send_message(customer.phone_number, text)
    # La IA no ve lo que se manda por fuera de ella (igual que quotes.set_quote).
    await shared_agent_runner().append_human_message(
        business_id=business.bussiness_id, conversation_id=conversation_id, user_id=customer.customer_id, text=text, by_system=True
    )
    return sent


class CatalogOrderHandler:
    async def maybe_handle(
        self,
        business: Businesses,
        customer: Customers,
        conversation: Conversations,
        message: Messages,
        order: Optional[dict],
    ) -> bool:
        """False si no es un carrito, o si se cargó completo y el agente debe
        seguir (con el resumen en message.content). True si ya quedó
        atendido aquí (espera al admin o el catálogo no es el del negocio)."""
        if not order:
            return False
        business_id = business.bussiness_id
        conversation_id = conversation.conversation_id
        configuration = TenantScopedQuery.get_one_for_business(Configurations, business_id)
        expected = configuration.whatsapp_catalog_id if configuration else None
        catalog_id = order.get("catalog_id")
        if not expected or catalog_id != expected:
            logger.warning(
                "Carrito de un catálogo que no es el del negocio",
                extra={"fields": {"business_id": business_id, "catalog_id": catalog_id, "esperado": expected}},
            )
            await _tell_customer(business, customer, conversation_id, WRONG_CATALOG)
            return True

        resolved, unknown, ignored = resolve_items(business_id, catalog_id, order.get("product_items") or [])
        lines = [{"producto": r["producto"], "cantidad": r["cantidad"], "precio": r["precio"]} for r in resolved]
        pending = [{"codigo": u["codigo"], "cantidad": u["cantidad"], "precio": u["precio"]} for u in unknown]
        result = await shared_agent_runner().edit_state(
            business_id, conversation_id, customer.customer_id,
            lambda state: order_tools.load_cart(state, message.whatsapp_message_id, lines, pending),
        )
        if result["ya_cargado"]:
            # Reintento: si el primer intento ya dejó el resumen, que lo conteste el flush.
            return message.content == CART_PLACEHOLDER
        logger.info(
            "Carrito del catálogo",
            extra={"fields": {"business_id": business_id, "resueltos": len(resolved), "desconocidos": len(unknown), "ignorados": len(ignored)}},
        )

        notifier = HandoffService()
        for entry in unknown:
            if register_unknown(business_id, catalog_id, entry, conversation_id):
                await notifier.notify_admins(business, _ask_admin_text(business_id, entry))
        for r in resolved:
            if r["precio_menu"] is not None and r["precio"] is not None and r["precio"] != r["precio_menu"]:
                await notifier.notify_admins(business, _price_text(customer, r, result))

        if unknown:
            text = CUSTOMER_WAIT + (f"\n{UNAVAILABLE}" if ignored else "")
            await _tell_customer(business, customer, conversation_id, text)
            return True

        message.content = _summary(result, ignored, (order.get("text") or "").strip())
        FireStoreCRUDService().update(message)
        return False


def _ask_admin_text(business_id: str, entry: dict) -> str:
    code = entry["codigo"]
    candidates = _candidates(business_id, entry["precio"])
    text = (
        f"🛒 Llegó un pedido del catálogo con un producto que no reconozco: código {code}, "
        f"precio {format_cop(entry['precio'], 'sin precio')}, cantidad {entry['cantidad']}. ¿Cuál es?"
    )
    if candidates:
        text += " Puede ser: " + ", ".join(candidates) + "."
    example = candidates[0].rsplit(" (", 1)[0] if candidates else "Hamburguesa"
    return text + f' Respóndeme por ejemplo "el {code} es {example}" o "el {code} no es del menú".'


def _price_text(customer: Customers, line: dict, result: dict) -> str:
    name = next((i["name"] for i in result["agregados"] if i["product"] == line["producto"]), line["producto"])
    return (
        f"🛒 El carrito de {customer.name or phone_for_humans(customer)} trae {name} a "
        f"{format_cop(line['precio'], '')} y en el menú está a {format_cop(line['precio_menu'], '')}. "
        "Se le cobra el del carrito (lo que vio el cliente): revisa el precio en el catálogo o en el menú."
    )


async def confirm_code(business: Businesses, mapping: CatalogMappings, producto: Optional[str], admin_id: Optional[str]) -> int:
    """Pasa el código a confirmed (producto) o ignored (None), y completa los
    carritos que lo esperaban. Devuelve a cuántos clientes se les avisó."""

    def _run(t):
        doc = CatalogMappings.collection.get(mapping.catalog_mapping_id, transaction=t)
        waiting = list(doc.esperando or [])
        doc.status = CONFIRMED if producto else IGNORED
        doc.producto = producto
        doc.confirmed_at = datetime.now(timezone.utc)
        doc.confirmed_by = admin_id
        doc.esperando = []
        doc.save(transaction=t)
        return waiting

    waiting = firestore_service.run_in_transaction(_run)
    notified = 0
    for conversation_id in waiting:
        conversation = Conversations.collection.get(conversation_id)
        if conversation is None or conversation.business_id != business.bussiness_id:
            continue
        customer = Customers.collection.get(conversation.customer_id)
        if customer is None:
            continue
        # ponytail: el carrito vive en la sesión del día; si el admin contesta
        # otro día, ya no está y el cliente no recibe nada.
        result = await shared_agent_runner().edit_state(
            business.bussiness_id, conversation_id, customer.customer_id,
            lambda state: order_tools.resolve_cart_code(state, mapping.product_retailer_id, producto),
        )
        if not result["habia"]:
            continue
        notified += await _tell_customer(business, customer, conversation_id, _resolved_text(result, producto is None))
    return notified
