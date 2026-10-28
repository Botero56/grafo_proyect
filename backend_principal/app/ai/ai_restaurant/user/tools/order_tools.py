"""El pedido como estado: un borrador estructurado en el state de la sesión
ADK (`pedido`) que el agente modifica con operaciones explícitas y que se
confirma desde ese mismo estado.

Antes el pedido vivía en el historial (resultados de tools que el modelo
tenía que recordar y volver a pasar) y la comanda la redactaba el modelo:
notas como "bien caldudos" se perdían y las correcciones dependían de su
memoria. Toda la validación, el domicilio, el guardado, el pago y la
impresión siguen siendo los de customer_tools: acá solo se orquestan.
"""

import copy
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

from google.adk.tools import ToolContext

from app.ai.ai_restaurant.user.tools import customer_tools
from app.firestore_db.firebase_schemas import Customers
from app.services import opening_hours, privacy, tenant_profile
from app.services.daily_menu.menu_diff import format_cop, format_fee
from app.services.delivery import couriers
from app.services.delivery.zones import candidate_zones, normalize
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.phones import is_username_id, normalize_phone

STATE_KEY = "pedido"
_HISTORY_KEY = "pedido_historial"
_MAX_HISTORY = 5


def _new_order(tool_context: ToolContext, previous: Optional[dict] = None) -> dict:
    """Borrador vacío. Nombre y dirección guardados del cliente (o del
    pedido anterior del día) quedan a la vista para confirmarlos, no se
    asumen."""
    customer = FireStoreCRUDService().get(Customers, tool_context.state.get("customer_id"))
    previous = previous or {}
    return {
        "items": [],
        "nombre": previous.get("nombre") or (customer.name if customer else None),
        # Escribe con usuario de WhatsApp: Meta no nos da su número y el
        # domiciliario necesita uno para llamarlo.
        "pide_telefono": bool(customer and is_username_id(customer.phone_number)),
        "telefono": previous.get("telefono") or (customer.contact_phone if customer else None),
        "direccion": None,
        # Con el barrio se encuentra la zona del domicilio (ver resolve_delivery_fee).
        "barrio": None,
        "direccion_guardada": previous.get("direccion") or (customer.address if customer else None),
        "referencias": None,
        "zona": None,
        "domicilio": None,
        "domicilio_estado": None,
        "forma_pago": None,
        "motivo_acuerdo": None,
        "notas_generales": None,
        "estado": "armando",
        "order_id": None,
    }


def _line(item: dict) -> dict:
    # precio_carrito solo lo pone el código (load_cart), nunca update_order.
    return {k: item.get(k) for k in ("product", "qty", "elecciones", "proteina", "notas", "precio_carrito")}


def _validate(item: dict, tool_context: ToolContext) -> tuple[Optional[dict], list[str]]:
    result = customer_tools.validate_order_items([_line(item)], tool_context)
    if not result["valid"]:
        return None, result["errors"]
    resolved = result["resolved_items"][0]
    if item.get("precio_carrito") is not None:
        resolved["precio_carrito"] = item["precio_carrito"]
    resolved["subtotal"] = resolved["unit_price"] * resolved["qty"]
    return resolved, []


def _add(order: dict, item: dict, tool_context: ToolContext) -> list[str]:
    resolved, errors = _validate(item, tool_context)
    if errors:
        return errors
    next_id = max((int(i["id"][1:]) for i in order["items"]), default=0) + 1
    order["items"].append({"id": f"i{next_id}", **resolved})
    return []


def _refresh_delivery(order: dict, tool_context: ToolContext) -> None:
    """El valor del domicilio lo decide el código (zona o cotización del
    equipo, que puede llegar en cualquier momento): se lee siempre de la
    conversación, nunca de lo que diga el modelo."""
    conversation = customer_tools._conversation(tool_context)
    order["domicilio"] = customer_tools._delivery_fee(conversation)
    order["domicilio_estado"] = conversation.delivery_status if conversation else None
    order["zona"] = conversation.delivery_zone if conversation else None


def _totals(order: dict) -> None:
    order["subtotal"] = sum(i["subtotal"] for i in order["items"])
    order["total"] = order["subtotal"] + (order["domicilio"] or 0)


# Nomenclatura urbana: [Tipo de vía] [Número] # [Vía de cruce] - [Placa]
# (ej. "Calle 10 # 20-30", "Cra 43A Bis # 12 Sur - 45", "Av 36A No 45-190").
# Unidad, torre y apto son opcionales (una casa no los tiene).
_VIA = (
    r"(avenida (calle|carrera)|calle|cll|cl|carrera|cra|kra|kr|cr|avenida|avda|av|ak|ac"
    r"|diagonal|diag|dg|transversal|transv|tv|tr|circular|cir|circunvalar|autopista|via|pasaje|psj)\.?"
)
_NUM = r"\d+ ?[a-z]?( ?bis)?( ?[a-z])?( ?(sur|norte|este|oeste))?"
# Tolera lo que se vio en clientes reales: "#-40-16" y "número 40 16".
_URBAN = re.compile(rf"\b{_VIA} ?{_NUM} ?(#|no\.?|n°|nro\.?|numero)? ?-? ?{_NUM} ?(- ?| )\d+")
# Unidad sin calle, igual de válida: [Nombre] Torre/Bloque [N] Apto/Casa [N]
# ("cerro claro torre 2 apto 2004"), o Conjunto/Edificio [Nombre] ... Apto [N].
# Sin el nombre ("torre 3 apto 402") no se sabe de qué unidad es.
_UNIT_NO = r"\b(apto|apartamento|apt|ap|casa)\.? ?\d+"
_UNIT = re.compile(
    rf"\b(?!(torre|bloque|interior|int)\b)[a-z]{{3,}}\b.*\b(torre|bloque|interior|int)\.? ?[a-z0-9]+\b.*{_UNIT_NO}"
    rf"|\b(conjunto|unidad|urbanizacion|edificio|ciudadela|condominio|residencial|torres)\b.*{_UNIT_NO}"
)
# Rural: no tiene nomenclatura; se acepta y la zona o el equipo la ubican.
_RURAL = re.compile(r"\b(vereda|finca|km|kilometro|corregimiento|parcela|hacienda)\b")
ADDRESS_FORMAT = (
    "[Tipo de vía] [Número] # [Vía de cruce] - [Placa], y si aplica [Nombre de la unidad], "
    "Torre [N], Apto [N] (ej. Calle 10 # 20-30, Conjunto Los Pinos, Torre 2, Apto 301); "
    "o, si vive en una unidad, solo [Nombre de la unidad] Torre [N] Apto [N] (ej. Cerro Claro Torre 2 Apto 2004)"
)


def valid_address(text: str) -> bool:
    """True si la dirección tiene nomenclatura (o es rural). "Mi casa",
    "donde siempre" o solo el barrio no sirven para entregar."""
    plain = unicodedata.normalize("NFKD", (text or "").lower())
    plain = " ".join("".join(c for c in plain if not unicodedata.combining(c)).split())
    return bool(_URBAN.search(plain) or _UNIT.search(plain) or _RURAL.search(plain))


def full_address(order: dict) -> str:
    """Dirección con su barrio, sin repetirlo si ya venía escrito en ella
    (ej. la dirección guardada de un pedido anterior)."""
    address, barrio = order.get("direccion") or "", order.get("barrio") or ""
    if not barrio or normalize(barrio) in normalize(address):
        return address
    return f"{address}, barrio {barrio}"


def _missing(order: dict) -> list[str]:
    missing = []
    if not order["items"]:
        missing.append("productos")
    if not order["direccion"]:
        missing.append("dirección de entrega")
    elif not order.get("barrio"):
        missing.append("barrio de la entrega (pídeselo: con el barrio se calcula el domicilio)")
    elif order.get("zona_opciones"):
        missing.append("barrio: el sistema le mandó una lista de barrios; pídele que elija el suyo ahí (lo resuelve el sistema)")
    elif order["domicilio"] is None:
        missing.append("valor del domicilio (lo resuelve el sistema)")
    if not order["forma_pago"]:
        missing.append("forma de pago")
    if order.get("pide_telefono") and not order.get("telefono"):
        missing.append("número de celular del cliente (escribe con usuario de WhatsApp y no tenemos su número: pídeselo)")
    if order.get("carrito_pendiente"):
        missing.append("productos del carrito que el restaurante está identificando (lo resuelve el sistema, no confirmes todavía)")
    return missing


def _find(order: dict, item_id: str) -> Optional[dict]:
    return next((i for i in order["items"] if i["id"] == item_id), None)


async def _apply(op: dict, order: dict, tool_context: ToolContext) -> list[str]:
    action = (op.get("accion") or "").strip().lower()

    if action == "agregar":
        item = {
            "product": op.get("producto"),
            "qty": op.get("cantidad", 1),
            "elecciones": op.get("elecciones") or {},
            "proteina": op.get("proteina"),
            "notas": op.get("notas"),
        }
        errors = _add(order, item, tool_context)
        waiting = order.get("por_elegir") or []
        match = next((p for p in waiting if p["producto"] == item["product"]), None)
        if not errors and match is not None:
            waiting.remove(match)
        return errors

    if action in ("modificar", "quitar"):
        current = _find(order, op.get("item_id") or "")
        if current is None:
            return [f"No hay ningún ítem '{op.get('item_id')}' en el pedido."]
        if action == "quitar":
            order["items"].remove(current)
            return []
        changed = dict(current)
        for key, field in (("cantidad", "qty"), ("elecciones", "elecciones"), ("proteina", "proteina"), ("notas", "notas")):
            if key in op:
                changed[field] = op[key]
        resolved, errors = _validate(changed, tool_context)
        if errors:
            return errors
        current.update(resolved)
        return []

    if action == "entrega":
        if op.get("referencias") is not None:
            order["referencias"] = op["referencias"]
        if (op.get("barrio") or "").strip():
            order["barrio"] = op["barrio"].strip()
        if (op.get("direccion") or "").strip():
            # Real 2026-10-21: "Mi casa" llegó a la comanda y el admin la
            # cotizó. Sin nomenclatura no se guarda ni se manda a cotizar.
            if not valid_address(op["direccion"]):
                return [f"Dirección incompleta: \"{op['direccion'].strip()}\". Pídele la dirección con este formato: {ADDRESS_FORMAT}."]
            order["direccion"] = op["direccion"].strip()
        # Sin barrio no se busca zona ni se le pide cotización al equipo:
        # _missing se lo hace pedir y llega en la siguiente "entrega".
        if not order["direccion"] or not order.get("barrio"):
            return []
        address = full_address(order)
        if order.get("zona_opciones") and order.get("zona_opciones_para") == address:
            return []  # ya tiene la lista de barrios para esta dirección: falta que elija
        order["zona_opciones"] = None
        customer_tools.upsert_customer_profile({"address": address}, tool_context)
        result = customer_tools.resolve_delivery_fee(address, tool_context, barrio=order["barrio"])
        if result.get("status") == "zona_desconocida":
            # Barrio corto o parecido a varias zonas ("Belén"): el cliente
            # elige en una lista (delivery/zone_choice.py). Si no se parece a
            # ninguna, o la lista no sale, cotiza un humano y el pedido espera.
            options = candidate_zones(order["barrio"], customer_tools.zones_for(tool_context.state.get("business_id")))
            if options and await customer_tools.ask_zone_choice(options, tool_context):
                order["zona_opciones"] = [z.get("nombre") for z in options]
                order["zona_opciones_para"] = address
            else:
                await customer_tools.request_delivery_quote(address, tool_context)
        elif result.get("status") == "error":
            return [result.get("reason") or "No se pudo resolver el domicilio."]
        return []

    if action == "cliente":
        name = (op.get("nombre") or "").strip()
        if name:
            order["nombre"] = name
            customer_tools.upsert_customer_profile({"name": name}, tool_context)
        return []

    if action == "telefono":
        phone = normalize_phone(op.get("numero"))
        # ponytail: solo celulares colombianos; un extranjero lo atiende un humano.
        if not (len(phone) == 12 and phone.startswith("573")):
            return ["Número no válido: debe ser un celular de 10 dígitos (ej. 3001234567)."]
        order["telefono"] = phone
        customer = FireStoreCRUDService().get(Customers, tool_context.state.get("customer_id"))
        if customer is not None:
            customer.contact_phone = phone
            customer.updated_at = datetime.now(timezone.utc)
            FireStoreCRUDService().update(customer)
        return []

    if action == "pago":
        method = (op.get("forma") or "").strip().lower()
        # "acuerdo" no es un medio de pago: lo decide la administración.
        allowed = tenant_profile.get_profile(tool_context.state.get("business_id"))["metodos_pago"] + ["acuerdo"]
        if method not in allowed:
            return [f"Forma de pago no válida: solo {', '.join(allowed)}."]
        order["forma_pago"] = method
        order["motivo_acuerdo"] = op.get("motivo") if method == "acuerdo" else None
        return []

    if action == "notas":
        order["notas_generales"] = op.get("texto") or None
        return []

    if action == "vaciar":
        order["items"] = []
        order["carrito_pendiente"] = []
        order["por_elegir"] = []
        return []

    return [f"Acción '{action}' no reconocida."]


async def update_order(operations: list[dict], tool_context: ToolContext) -> dict:
    """Aplica cambios al pedido en curso del cliente. Llámala en el MISMO
    turno en que el cliente diga algo de su pedido: productos, cambios,
    notas, dirección, nombre o forma de pago. Nada de eso debe quedar solo en
    la conversación.

    Args:
        operations: lista de operaciones, en orden. Cada una es un dict con
            "accion" y sus campos:
            - {"accion": "agregar", "producto": "menu_del_dia" | "otras_opciones" | "fixed:...",
               "cantidad": 1, "elecciones": {"Sopa": "Frijoles"}, "proteina": "Pollo",
               "notas": "frijoles bien caldudos"}
              `proteina` solo para "otras_opciones". `notas` es OBLIGATORIO
              cuando el cliente da un detalle de ese producto (cómo lo
              quiere, sin X, extra Y, alergias): copia sus palabras.
            - {"accion": "modificar", "item_id": "i1", "cantidad"?, "elecciones"?, "proteina"?, "notas"?}
            - {"accion": "quitar", "item_id": "i1"}
            - {"accion": "entrega", "direccion": "...", "barrio": "...", "referencias": "..."}
              `direccion` debe tener nomenclatura (tipo de vía, número, #
              cruce - placa: "Calle 10 # 20-30, Torre 2, Apto 301") o ser
              una unidad con torre y apto ("Cerro Claro Torre 2 Apto 2004");
              "mi casa" o solo el barrio se rechazan.
              `barrio` es obligatorio: sin él no se calcula el domicilio. Si
              el cliente lo dijo dentro de la dirección, cópialo aquí; si no
              lo sabe, pon lo que diga (ej. "no sabe") y el equipo lo cotiza.
            - {"accion": "cliente", "nombre": "..."}
            - {"accion": "telefono", "numero": "3001234567"}  # solo si "falta" lo pide
            - {"accion": "pago", "forma": "efectivo" | "transferencia" | "acuerdo",
               "motivo": "tengo tiquetera"}  # motivo solo con "acuerdo"
            - {"accion": "notas", "texto": "..."}  # notas del pedido completo
            - {"accion": "deshacer"}  # revierte el último cambio (va sola)
            - {"accion": "vaciar"}

    Returns:
        {"ok": bool, "errores": [...], "pedido": {...}, "falta": [...]}.
        `pedido` es el estado completo después de los cambios: precios,
        domicilio y total salen de acá, nunca de tu memoria. Si hay errores,
        los demás cambios igual se aplicaron; cuéntale al cliente qué falta.
    """
    state = tool_context.state
    order = state.get(STATE_KEY)

    if operations and all((op.get("accion") or "").lower() == "deshacer" for op in operations):
        history = list(state.get(_HISTORY_KEY) or [])
        if not history:
            return {"ok": False, "errores": ["No hay cambios para deshacer."], "pedido": order, "falta": []}
        order = history.pop()
        state[_HISTORY_KEY] = history
        state[STATE_KEY] = order
        return {"ok": True, "errores": [], "pedido": order, "falta": _missing(order) if order else []}

    if order and order["estado"] == "confirmado" and operations and all(
        (op.get("accion") or "").lower() == "pago" for op in operations
    ):
        # Acuerdo de pago que la administración decidió cobrar: la forma de
        # pago va al pedido ya guardado, no abre uno nuevo.
        method = (operations[-1].get("forma") or "").strip().lower()
        payment = customer_tools.set_payment_method(order["order_id"], method, tool_context)
        if not payment.get("ok"):
            return {"ok": False, "errores": [payment.get("reason")], "pedido": order, "falta": []}
        order["forma_pago"] = method
        state[STATE_KEY] = order
        reply = {"ok": True, "errores": [], "pedido": order, "falta": [], "monto_a_transferir": payment["amount_to_pay"]}
        if method == "transferencia":
            reply["cuentas_transferencia"] = _transfer_accounts(tool_context)
        return reply

    if order is None or (order["estado"] == "confirmado" and operations):
        # Un pedido nuevo después de uno confirmado: se conserva quién es y
        # dónde vive, el resto empieza de cero.
        order = _new_order(tool_context, previous=order)
    state[_HISTORY_KEY] = (list(state.get(_HISTORY_KEY) or []) + [copy.deepcopy(state.get(STATE_KEY))])[-_MAX_HISTORY:]

    errors: list[str] = []
    for op in operations:
        errors.extend(await _apply(op, order, tool_context))

    _refresh_delivery(order, tool_context)
    _totals(order)
    # Reasignar (no mutar en sitio) es lo que hace que ADK registre el cambio.
    state[STATE_KEY] = order
    return {"ok": not errors, "errores": errors, "pedido": order, "falta": _missing(order)}


def _ticket_text(order: dict) -> str:
    """La sección PEDIDO de la comanda, armada desde el estado: cada nota
    del cliente llega a cocina tal cual la dijo."""
    blocks = []
    for item in order["items"]:
        lines = [f"{item['qty']}x {item['name']}"]
        lines += [f"{k}: {v}" for k, v in (item.get("elecciones") or {}).items()]
        if item.get("notas"):
            lines.append(f"Nota: {item['notas']}")
        blocks.append("\n".join(lines))
    if order.get("notas_generales"):
        blocks.append(f"Nota del pedido: {order['notas_generales']}")
    if order.get("referencias"):
        blocks.append(f"Referencias: {order['referencias']}")
    return "\n\n".join(blocks)


def _transfer_accounts(tool_context: ToolContext) -> list[dict]:
    """Cuentas del negocio tal cual están configuradas: la IA las copia al
    cliente, no las arma (Configurations.cuentas_transferencia)."""
    return tenant_profile.get_profile(tool_context.state.get("business_id"))["cuentas_transferencia"]


async def confirm_order(tool_context: ToolContext) -> dict:
    """Confirma el pedido en curso: lo guarda, registra la forma de pago y
    manda la comanda. Llámala SOLO después de mostrarle al cliente el
    resumen completo (ítems con sus notas, domicilio, total y forma de pago)
    y de que él diga explícitamente que sí.

    Returns:
        {"ok": True, "order_id", "total", "forma_pago", "comanda": "impresa" |
         "en_espera" | "error", "monto_a_transferir"?, "cuentas_transferencia"?}:
        - transferencia: dile el monto_a_transferir EXACTO y los datos de
          cuentas_transferencia tal cual (banco, tipo, número, titular, llave),
          sin cambiar ni un dígito, y pídele la foto del comprobante por este
          chat; nunca digas que el pago está validado.
        - acuerdo: dile que estás confirmando con la administración; no le
          pidas pago.
        - efectivo: confírmale que su pedido quedó confirmado.
        {"ok": False, "falta" | "errores": [...]} si no se puede confirmar
        todavía: pide lo que falta.
    """
    state = tool_context.state
    order = state.get(STATE_KEY)
    if not order or not order["items"]:
        return {"ok": False, "falta": ["productos"]}
    if order["estado"] == "confirmado":
        return {"ok": True, "ya_confirmado": True, "order_id": order["order_id"], "total": order.get("total")}

    _refresh_delivery(order, tool_context)
    missing = _missing(order)
    if missing:
        return {"ok": False, "falta": missing}
    minimum = tenant_profile.get_profile(tool_context.state.get("business_id"))["pedido_minimo"]
    if minimum and order["subtotal"] < minimum:
        return {"ok": False, "errores": [f"El pedido mínimo es {format_cop(minimum, '')} sin domicilio."]}
    business_id = tool_context.state.get("business_id")
    hours = opening_hours.status(business_id, tenant_profile.get_profile(business_id)["opening_hours"])
    if hours["enforce"] and not hours["open"]:
        when = opening_hours.describe_next_opening(hours["next_opening"], datetime.now(timezone.utc))
        return {"ok": False, "errores": [f"El negocio está cerrado ahora; abre {when}. No se puede confirmar el pedido todavía."]}
    # Ley 1581: sin la autorización de datos no se guarda el pedido. Se le
    # pregunta una vez por pedido con botones; el clic lo atiende privacy.py.
    customer = FireStoreCRUDService().get(Customers, state.get("customer_id"))
    if not privacy.has_consent(customer):
        if not order.get("autorizacion_pedida"):
            order["autorizacion_pedida"] = await privacy.ask(business_id, state.get("customer_id"))
            state[STATE_KEY] = order
        return {
            "ok": False,
            "falta": ["autorizacion_datos"],
            "nota": "Le llegó un mensaje con botones para aceptar la política de datos. Dile que toque 'Acepto' "
            "para confirmar su pedido. No lo des por confirmado hasta que acepte.",
        }

    # save_order revalida contra el menú de este momento (el admin pudo corregirlo).
    address = full_address(order) + (f" ({order['referencias']})" if order.get("referencias") else "")
    saved = customer_tools.save_order(
        {"items": [_line(i) for i in order["items"]], "address": address, "notes": order.get("notas_generales")},
        tool_context,
    )
    if not saved.get("order_id"):
        return {"ok": False, "errores": saved.get("errors") or [saved.get("reason") or "No se pudo guardar el pedido."]}

    # Confirmado apenas existe en Orders: un reintento nunca duplica el pedido.
    order.update(estado="confirmado", order_id=saved["order_id"], total=saved["total"])
    state[STATE_KEY] = order
    # Sin esto, "deshacer" volvía a un borrador sin order_id y se podía confirmar dos veces.
    state[_HISTORY_KEY] = []
    couriers.end_customer_mode(state.get("customer_id"))

    reply = {"ok": True, "order_id": saved["order_id"], "total": saved["total"], "forma_pago": order["forma_pago"]}
    if order["forma_pago"] == "acuerdo":
        await customer_tools.request_payment_agreement(saved["order_id"], order.get("motivo_acuerdo") or "", tool_context)
    else:
        payment = customer_tools.set_payment_method(saved["order_id"], order["forma_pago"], tool_context)
        if order["forma_pago"] == "transferencia" and payment.get("ok"):
            reply["monto_a_transferir"] = payment["amount_to_pay"]
            reply["cuentas_transferencia"] = _transfer_accounts(tool_context)

    ticket = await customer_tools.print_order_ticket(saved["order_id"], _ticket_text(order), tool_context)
    reply["comanda"] = "impresa" if ticket.get("printed") else "en_espera" if ticket.get("held") else "error"
    return reply


def render_state(state: dict) -> str:
    """Lo que el agente de clientes ve en CADA llamada al modelo: las reglas
    del negocio y el pedido en curso. Así nunca depende de recordar mensajes
    viejos ni de reglas quemadas en el prompt."""
    blocks = (tenant_profile.render_profile(state.get("business_id")), render_order_state(state))
    return "\n\n".join(block for block in blocks if block)


def render_order_state(state: dict) -> str:
    order = state.get(STATE_KEY)
    if not order:
        return ""
    lines = [f"Pedido ({order['estado']}{', #' + order['order_id'][-6:].upper() if order.get('order_id') else ''}):"]
    for item in order["items"]:
        detail = ", ".join(f"{k}: {v}" for k, v in (item.get("elecciones") or {}).items())
        note = f' — nota: "{item["notas"]}"' if item.get("notas") else ""
        lines.append(f"- [{item['id']}] {item['qty']}x {item['name']}{' (' + detail + ')' if detail else ''}{note} — {format_cop(item['subtotal'], '')}")
    for choose in order.get("por_elegir") or []:
        lines.append(f"- {choose['cantidad']}x {choose['producto']} pedido desde el carrito, AÚN NO está en el pedido: pregúntale lo que falte (sopa, proteína) y agrégalo con update_order")
    for pending in order.get("carrito_pendiente") or []:
        lines.append(f"- {pending['cantidad']}x producto del carrito que el restaurante está identificando — {format_cop(pending['precio'] * pending['cantidad'], '')}")
    if not order["items"] and not order.get("carrito_pendiente"):
        lines.append("- (sin productos)")
    if order.get("nombre"):
        lines.append(f"Nombre: {order['nombre']}")
    if order.get("telefono"):
        lines.append(f"Celular: {order['telefono']}")
    if order.get("direccion"):
        fee = format_fee(order["domicilio"], "") if order.get("domicilio") is not None else f"pendiente ({order.get('domicilio_estado') or 'sin resolver'})"
        lines.append(f"Dirección: {full_address(order)} · domicilio: {fee}")
    elif order.get("direccion_guardada"):
        lines.append(f"Dirección guardada (confírmala con el cliente): {order['direccion_guardada']}")
    if order.get("referencias"):
        lines.append(f"Referencias: {order['referencias']}")
    lines.append(f"Forma de pago: {order.get('forma_pago') or '(falta)'}")
    if order.get("notas_generales"):
        lines.append(f'Notas del pedido: "{order["notas_generales"]}"')
    if order.get("total") is not None and order["items"]:
        lines.append(f"Total: {format_cop(order['total'], '')}")
    missing = _missing(order) if order["estado"] == "armando" else []
    if missing:
        lines.append("Falta: " + ", ".join(missing))
    return "\n".join(lines)


# Del carrito no se pueden cargar solos (faltan sopa y proteína): quedan en
# "por_elegir" hasta que el agente los agrega con update_order.
_DAY_PRODUCTS = ("menu_del_dia", "otras_opciones")


class _StateContext:
    """Para tocar el pedido por fuera de un turno (carrito del catálogo):
    estas funciones solo leen `state` del ToolContext."""

    def __init__(self, state: dict) -> None:
        self.state = state


def _cart_item(producto: str, cantidad: int, precio: Optional[float]) -> dict:
    return {"product": producto, "qty": cantidad, "elecciones": {}, "proteina": None, "notas": None, "precio_carrito": precio}


def load_cart(state: dict, wamid: str, lines: list[dict], pending: list[dict]) -> tuple[Optional[dict], dict]:
    """Suma al borrador un carrito del catálogo ya resuelto por código.

    lines: [{"producto": "fixed:...", "cantidad": 1, "precio": 18000}] -- se
        cobra el precio del carrito, que es el que vio el cliente.
    pending: [{"codigo", "cantidad", "precio"}] que el admin aún no identificó;
        bloquean confirm_order (ver _missing).

    Devuelve (cambios del state o None, resultado). El mismo wamid dos veces
    no carga nada (reintento de Cloud Tasks)."""
    order = state.get(STATE_KEY)
    if order and wamid in (order.get("carritos") or []):
        return None, {"ya_cargado": True, "agregados": [], "errores": []}
    ctx = _StateContext(state)
    history = (list(state.get(_HISTORY_KEY) or []) + [copy.deepcopy(order)])[-_MAX_HISTORY:]
    if order is None or order["estado"] == "confirmado":
        order = _new_order(ctx, previous=order)

    added, errors, to_choose = [], [], []
    for line in lines:
        if line["producto"] in _DAY_PRODUCTS:
            to_choose.append({"producto": line["producto"], "cantidad": line["cantidad"]})
            continue
        problems = _add(order, _cart_item(line["producto"], line["cantidad"], line["precio"]), ctx)
        errors += problems
        if not problems:
            added.append(order["items"][-1])
    order["por_elegir"] = (order.get("por_elegir") or []) + to_choose
    order["carrito_pendiente"] = (order.get("carrito_pendiente") or []) + pending
    order["carritos"] = (order.get("carritos") or []) + [wamid]
    _refresh_delivery(order, ctx)
    _totals(order)
    return {STATE_KEY: order, _HISTORY_KEY: history}, {
        "ya_cargado": False, "agregados": added, "errores": errors, "por_elegir": to_choose, "pedido": order,
    }


def resolve_cart_code(state: dict, codigo: str, producto: Optional[str]) -> tuple[Optional[dict], dict]:
    """El admin identificó `codigo`: sale de carrito_pendiente y, si es del
    menú (producto no None), entra al pedido con el precio del carrito."""
    order = state.get(STATE_KEY)
    mine = [p for p in ((order or {}).get("carrito_pendiente") or []) if p["codigo"] == codigo]
    if not mine:
        return None, {"habia": False}
    ctx = _StateContext(state)
    order["carrito_pendiente"] = [p for p in order["carrito_pendiente"] if p["codigo"] != codigo]
    added, errors, to_choose = [], [], []
    for p in mine:
        if producto in _DAY_PRODUCTS:
            to_choose.append({"producto": producto, "cantidad": p["cantidad"]})
        elif producto:
            problems = _add(order, _cart_item(producto, p["cantidad"], p["precio"]), ctx)
            errors += problems
            if not problems:
                added.append(order["items"][-1])
    order["por_elegir"] = (order.get("por_elegir") or []) + to_choose
    _refresh_delivery(order, ctx)
    _totals(order)
    return {STATE_KEY: order}, {
        "habia": True, "agregados": added, "errores": errors, "por_elegir": to_choose,
        "quedan": len(order["carrito_pendiente"]), "pedido": order,
    }
