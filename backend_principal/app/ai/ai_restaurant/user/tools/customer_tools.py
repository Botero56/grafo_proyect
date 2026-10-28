"""
Tools usadas por el customer_agent: menú, perfil del cliente y ciclo de pedido.
"""

from datetime import datetime, timezone
from typing import Optional

from google.adk.tools import ToolContext

from app.firestore_db.firebase_schemas import (
    Businesses,
    Configurations,
    Conversations,
    Customers,
    Orders,
    PrintJobs,
)
from app.schemas.enum_schemas import (
    DeliveryStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    PriceType,
    PrintJobStatus,
)
from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia
from app.services.daily_menu import menu_image, stock
from app.services.daily_menu.daily_menu_service import DailyMenuService
from app.services.daily_menu.menu_diff import format_fee
from app.services.delivery import incidents, quotes, tracking
from app.services.payments import payment_agreement
from app.services.delivery.zones import CHOICE_NONE, CHOICE_PREFIX, find_zone, normalize, zones_for
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services.printer_client.print_jobs import build_ticket, send_print_job
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient


def _fixed_price(tipo_precio: str, precio):
    """El precio solo se expone si está confirmado; si no, None -- el
    modelo nunca debe ver un número que no pueda cobrar."""
    return precio if tipo_precio == PriceType.FIJO.value else None


def get_active_menu(tool_context: ToolContext) -> dict:
    """Obtiene el menú del día vigente HOY del restaurante: dos productos
    del día, "menu_del_dia" (el menú del día -- lo que un cliente pide
    cuando pregunta por "el menú" o "el menú del día") y "otras_opciones",
    que comparten los mismos acompañamientos, más los `extras_fijos`.

    El business_id no lo decide el modelo: ya lo resolvió el webhook y viaja
    en el state de la sesión (ver AgentRunnerService.get_reply), por eso se
    lee de tool_context en vez de pedirlo como argumento.

    El menú del día se carga por imagen desde el chat del admin (ver
    app/services/daily_menu/); si el admin todavía no lo ha confirmado hoy,
    NO existe un menú "activo" para la fecha de hoy -- eso se refleja en
    `menu_available=False`, nunca se inventa ni se reutiliza el de un día
    anterior.

    Returns:
        Si el admin avisó que el negocio no abre hoy (ver mark_closed_today
        del admin_agent):
            {"menu_available": False, "closed_today": True,
             "closed_reason": "..." | None}
        Si no hay menú activo hoy (y no está marcado como cerrado):
            {"menu_available": False, "closed_today": False,
             "expected_ready_time": "09:00" | None,
             "extras_fijos": [...]}  # igual que abajo, se pueden pedir
        Si hay menú activo hoy:
            {"menu_available": True,
             "menu_del_dia": {"id": "menu_del_dia", "nombre": "...",
                               "price_type": "fijo"|"no_legible",
                               "price": 18500 | None},
             "otras_opciones": {"id": "otras_opciones",
                                 "proteinas": ["Cerdo", "Pollo", ...],
                                 "price_type": "fijo"|"no_legible",
                                 "price": 20500 | None},
             "acompanamientos": [{"nombre": "Sopa", "opciones": ["Ajiaco", "Frijoles"]}, ...],
             "envios": True | False | None,
             "telefono_contacto": "..." | None,
             "extras_fijos": [{"id": "fixed:...", "name": "...",
                                "description": "..." | None,
                                "category": "...",
                                "price_type": "fijo"|"no_legible",
                                "price": ... | None}, ...]}
        "menu_del_dia" ES el menú del día -- si el cliente pregunta "¿cuál
        es el menú?" o "¿qué hay de menú del día?", la respuesta sale de
        acá directo, sin pedirle que aclare a qué se refiere.
        `price_type` "no_legible" en menu_del_dia u otras_opciones significa
        que ese precio NO está confirmado hoy: nunca inventes ni asumas un
        número, usa request_human_handoff con motivo="sin_datos".
        `extras_fijos` son productos configurados aparte de la imagen
        (productos fijos y los del catálogo de WhatsApp, ver
        FixedMenuItems). Se pueden pedir solos o junto al menú, aunque no
        haya menú del día: en update_order van con su `id` como
        "producto".
        `stock_hoy` (solo si el admin limitó algo hoy): {"Pollo": 0,
        "Hamburguesa": 3} -- 0 = agotado, n = solo quedan n. Lo que no
        aparece no tiene límite.
    """
    return active_menu(tool_context.state.get("business_id"))


def active_menu(business_id: str) -> dict:
    """Lo mismo que get_active_menu, para usarlo fuera de un turno del
    cliente (ej. el admin marcando algo agotado)."""
    today = resolve_fecha_vigencia(None)
    service = DailyMenuService()

    closed = service.get_closed_for_date(business_id, today)
    if closed is not None:
        return {"menu_available": False, "closed_today": True, "closed_reason": closed.caption}

    fixed_items = service.active_fixed_items(business_id)
    extras_fijos = [
        {
            "id": f"fixed:{item.fixed_item_id}",
            "name": item.nombre,
            "description": item.descripcion,
            "category": item.categoria,
            "price_type": item.tipo_precio,
            "price": _fixed_price(item.tipo_precio, item.precio),
        }
        for item in fixed_items
    ]

    daily_menu = service.get_active_for_date(business_id, today)
    stock_hoy = stock.stock_view(stock.today_stock(business_id))

    if daily_menu is None:
        configuration = TenantScopedQuery.get_one_for_business(Configurations, business_id)
        ready_time = (
            getattr(configuration, "daily_menu_expected_ready_time", None) if configuration else None
        )
        # Los extras (productos fijos y del catálogo de WhatsApp) no
        # dependen de la foto del día: se pueden pedir igual.
        return {
            "menu_available": False,
            "closed_today": False,
            "expected_ready_time": ready_time,
            "extras_fijos": extras_fijos,
            **({"stock_hoy": stock_hoy} if stock_hoy else {}),
        }

    extracted = DailyMenuService.get_extracted_menu(daily_menu)

    return {
        "menu_available": True,
        "menu_del_dia": {
            "id": "menu_del_dia",
            "nombre": extracted.menu_del_dia.nombre,
            "price_type": extracted.menu_del_dia.tipo_precio,
            "price": _fixed_price(extracted.menu_del_dia.tipo_precio, extracted.menu_del_dia.precio),
        },
        "otras_opciones": {
            "id": "otras_opciones",
            "proteinas": extracted.otras_opciones.proteinas,
            "price_type": extracted.otras_opciones.tipo_precio,
            "price": _fixed_price(
                extracted.otras_opciones.tipo_precio, extracted.otras_opciones.precio
            ),
        },
        "acompanamientos": [
            {"nombre": a.nombre, "opciones": a.opciones} for a in extracted.acompanamientos
        ],
        "envios": extracted.envios,
        "telefono_contacto": extracted.telefono_contacto,
        "extras_fijos": extras_fijos,
        **({"stock_hoy": stock_hoy} if stock_hoy else {}),
    }


_UPSERTABLE_CUSTOMER_FIELDS = {"name", "address", "notes"}


def upsert_customer_profile(updates: dict, tool_context: ToolContext) -> dict:
    """Crea o completa datos del cliente que falten (dirección, notas, etc.).

    El customer_id sale del state de la sesión (lo deja el backend),
    no se le pide al modelo. El cliente ya existe en Firestore (lo crea
    WhatsAppWebhookService al recibir el primer mensaje), así que esto
    siempre actualiza, nunca crea.

    Args:
        updates: diccionario con los campos a crear/actualizar, ej.
            {"name": "...", "address": "..."}. Solo se guardan los campos
            reconocidos: name, address, notes.

    Returns:
        dict con el perfil actualizado.
    """
    customer_id = tool_context.state.get("customer_id")

    customer = FireStoreCRUDService().get(Customers, customer_id)
    if customer is None:
        return {"exists": False, "customer_id": customer_id}

    for field, value in updates.items():
        if field in _UPSERTABLE_CUSTOMER_FIELDS:
            setattr(customer, field, value)

    customer.updated_at = datetime.now(timezone.utc)
    FireStoreCRUDService().update(customer)

    return {
        "exists": True,
        "customer_id": customer.customer_id,
        "name": customer.name,
        "address": customer.address,
        "notes": customer.notes,
    }


_PRODUCT_KEYS = {"menu_del_dia", "otras_opciones"}
# Tope por línea de pedido; un pedido más grande lo atiende un humano.
_MAX_QTY = 20


def validate_order_items(items: list[dict], tool_context: ToolContext) -> dict:
    """Valida un pedido contra el menú del día vigente AHORA.

    Productos pedibles: "menu_del_dia" y "otras_opciones" (solo si hay
    menú del día hoy) y los `extras_fijos` de get_active_menu, por su id
    ("fixed:..."), que no llevan elecciones ni proteína y se pueden pedir
    aunque no haya menú del día. El menú lo lee esta misma tool (igual que
    get_active_menu), no lo recibe del modelo: así nunca se valida contra un
    menú viejo que quedó en el historial de la conversación -- un precio que
    el admin corrigió después, o productos de otro día. Cruza lo que el
    cliente pidió contra ese menú para que el modelo nunca invente un
    producto, una elección de acompañamiento o una proteína que no está hoy.

    Args:
        items: lista de líneas de pedido, cada una:
            {"product": "menu_del_dia" | "otras_opciones" | "fixed:...",
             "qty": 1,
             "elecciones": {"Sopa": "Ajiaco", ...},  # una por cada
                 acompañamiento que tenga "opciones" en el menú (ver
                 get_active_menu) -- si un acompañamiento no tiene
                 opciones (ej. "Arroz"), no hace falta elegirlo.
             "proteina": "Pollo"}  # SOLO para "otras_opciones"

    Returns:
        dict con:
            - valid: bool
            - errors: lista de mensajes (producto no existe hoy, precio no
              legible, falta elegir un acompañamiento, elección inválida,
              falta la proteína o no está disponible hoy, etc.)
            - resolved_items: líneas ya validadas, con name/unit_price/qty/
              elecciones/proteina, listas para
              save_order.

    Un producto con price_type "no_legible" SIEMPRE cuenta como error acá
    -- nunca se agrega al pedido con un precio inventado. Si el cliente
    insiste, usa request_human_handoff (motivo="sin_datos") en vez de
    seguir intentando con validate_order_items para ese producto.
    """
    errors: list[str] = []
    resolved_items: list[dict] = []

    menu = get_active_menu(tool_context)
    if menu.get("closed_today"):
        return {"valid": False, "errors": ["El negocio no abre hoy."], "resolved_items": []}
    extras = {extra["id"]: extra for extra in menu.get("extras_fijos") or []}

    # (nombre original, clave en minúscula para matchear, opciones válidas
    # en minúscula) -- se guarda el nombre original aparte para que los
    # mensajes de error digan "Sopa", no "sopa".
    acompanamientos_con_opciones = [
        (a["nombre"], a["nombre"].strip().lower(), [o.strip().lower() for o in a.get("opciones", [])])
        for a in menu.get("acompanamientos", [])
        if a.get("opciones")
    ]

    for requested in items:
        product = requested.get("product")
        qty = requested.get("qty", 1)

        # La cantidad multiplica el precio: una negativa, fraccionaria o
        # absurda (prompt injection) cambiaría el total que se cobra.
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or qty != int(qty) or not 1 <= qty <= _MAX_QTY:
            errors.append(f"Cantidad inválida para '{product}': debe ser un número entero entre 1 y {_MAX_QTY}.")
            continue
        qty = int(qty)

        extra = extras.get(product)
        if extra is not None:
            # Carrito del catálogo: se cobra el precio que vio el cliente. Solo
            # lo pone el código (order_tools.load_cart); el modelo no llega acá.
            price = requested.get("precio_carrito")
            if price is None:
                price = extra.get("price")
            if price is None:
                errors.append(
                    f"El precio de '{extra['name']}' no está confirmado -- "
                    "no se puede agregar al pedido sin confirmarlo con un humano."
                )
                continue
            resolved_items.append(
                {
                    "product": product,
                    "name": extra["name"],
                    "unit_price": price,
                    "qty": qty,
                    "elecciones": {},
                    "proteina": None,
                    "notas": requested.get("notas"),
                }
            )
            continue

        if product not in _PRODUCT_KEYS:
            errors.append(
                f"'{product}' no es un producto válido (solo menu_del_dia, otras_opciones "
                "o el id de un extra de get_active_menu)."
            )
            continue
        if not menu.get("menu_available"):
            errors.append(f"Hoy todavía no hay menú del día: '{product}' no está disponible.")
            continue

        product_data = menu.get(product)
        if not product_data:
            errors.append(f"Hoy no hay '{product}' disponible.")
            continue
        if product_data.get("price_type") != "fijo" or product_data.get("price") is None:
            errors.append(
                f"El precio de '{product}' no está confirmado hoy (precio no legible) -- "
                "no se puede agregar al pedido sin confirmarlo con un humano."
            )
            continue

        elecciones_solicitadas = requested.get("elecciones") or {}
        elecciones_normalizadas: dict[str, str] = {}
        item_valido = True

        for acomp_nombre, acomp_key, opciones_validas in acompanamientos_con_opciones:
            elegido = next(
                (v for k, v in elecciones_solicitadas.items() if k.strip().lower() == acomp_key),
                None,
            )
            if elegido is None:
                errors.append(f"Falta elegir '{acomp_nombre}' para tu pedido.")
                item_valido = False
            elif elegido.strip().lower() not in opciones_validas:
                errors.append(f"'{elegido}' no es una opción válida para '{acomp_nombre}' hoy.")
                item_valido = False
            else:
                elecciones_normalizadas[acomp_nombre] = elegido

        proteina = requested.get("proteina")
        if product == "otras_opciones":
            proteinas_hoy = [p.strip().lower() for p in product_data.get("proteinas", [])]
            if not proteina:
                errors.append("Falta elegir la proteína para 'otras_opciones'.")
                item_valido = False
            elif proteina.strip().lower() not in proteinas_hoy:
                errors.append(f"'{proteina}' no está entre las proteínas de hoy.")
                item_valido = False

        if not item_valido:
            continue

        resolved_items.append(
            {
                "product": product,
                "name": proteina if product == "otras_opciones" else product_data.get("nombre"),
                "unit_price": product_data["price"],
                "qty": qty,
                "elecciones": elecciones_normalizadas,
                "proteina": proteina if product == "otras_opciones" else None,
                # La nota del cliente viaja con su línea hasta Orders y la comanda.
                "notas": requested.get("notas"),
            }
        )

    # Lo agotado o limitado por el admin (ver stock.py), sumando las líneas
    # del mismo producto.
    errors += stock.shortages(
        stock.today_stock(tool_context.state.get("business_id")),
        [(item["name"], item["qty"]) for item in resolved_items],
    )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "resolved_items": resolved_items,
    }


def _conversation(tool_context: ToolContext) -> Optional[Conversations]:
    # La sesión ADK es por día (ver agent_runner._daily_session_id): el id
    # real de la conversación viaja en el state. Las sesiones creadas antes
    # de ese cambio no lo tienen, y ahí su id sí era el de la conversación.
    conversation_id = tool_context.state.get("conversation_id") or tool_context.session.id
    return Conversations.collection.get(conversation_id)


def _delivery_fee(conversation: Optional[Conversations]) -> Optional[float]:
    """Tarifa ya resuelta por el código (zona o cotización), o None."""
    if conversation is None or conversation.delivery_fee is None:
        return None
    if conversation.delivery_status not in (DeliveryStatus.ZONA.value, DeliveryStatus.COTIZADO.value):
        return None
    return conversation.delivery_fee


def resolve_delivery_fee(address: str, tool_context: ToolContext, barrio: Optional[str] = None) -> dict:
    """Determina la zona y el valor del domicilio de una dirección.

    Llámala cada vez que el cliente confirme o dé la dirección de entrega,
    antes de calcular el total. El valor lo decide el sistema: nunca lo
    inventes ni lo calcules tú.

    Args:
        address: la dirección tal cual la dio el cliente. Si el cliente te
            dijo después en qué zona queda, inclúyela (ej. "Cerro Azul,
            torre 3 apto 402").
        barrio: el barrio que dio el cliente. La zona se busca primero en
            él (es lo que nombran las zonas) y, si no da una sola, en la
            dirección completa: así una calle que se llama como otra zona
            no confunde la tarifa.

    Returns:
        - {"status": "zona", "zone": "...", "delivery_fee": 3000}: listo.
        - {"status": "zona_desconocida", "zonas": [...]}: la dirección no
          cae en ninguna zona. No le preguntes al cliente por zonas: llama
          a request_delivery_quote para que un humano la verifique.
        - {"status": "cotizacion_pendiente"}: un humano está cotizando el
          domicilio de esta dirección; dile al cliente que en un momento le
          confirman el valor.
        - {"status": "cotizado", "delivery_fee": ...}: el valor que cotizó
          el equipo para esta dirección; ya puedes seguir con el pedido.
    """
    conversation = _conversation(tool_context)
    if conversation is None:
        return {"status": "error", "reason": "Conversación no encontrada."}

    same_address = normalize(conversation.delivery_address or "") == normalize(address)
    if same_address and conversation.delivery_status == DeliveryStatus.COTIZADO.value:
        return {"status": "cotizado", "delivery_fee": conversation.delivery_fee}
    if same_address and conversation.delivery_status == DeliveryStatus.PENDIENTE_COTIZACION.value:
        return {"status": "cotizacion_pendiente"}

    zones = zones_for(tool_context.state.get("business_id"))
    # La zona que ya quedó para esta misma dirección (sola, o la que eligió
    # el cliente) se reusa con su tarifa de hoy; si la zona ya no existe, se
    # vuelve a buscar.
    remembered = None
    if same_address and conversation.delivery_status == DeliveryStatus.ZONA.value:
        remembered = next((z for z in zones if z.get("nombre") == conversation.delivery_zone), None)
    zone = remembered or (find_zone(barrio, zones) if barrio else None) or find_zone(address, zones)

    conversation.delivery_address = address
    if zone is None:
        # Sin zona no queda ninguna tarifa vieja que el total
        # pueda usar por error.
        conversation.delivery_zone = None
        conversation.delivery_fee = None
        conversation.delivery_status = None
        FireStoreCRUDService().update(conversation)
        return {"status": "zona_desconocida", "zonas": [z.get("nombre") for z in zones]}

    conversation.delivery_zone = zone.get("nombre")
    conversation.delivery_fee = zone.get("tarifa")
    conversation.delivery_status = DeliveryStatus.ZONA.value
    FireStoreCRUDService().update(conversation)
    return {"status": "zona", "zone": conversation.delivery_zone, "delivery_fee": conversation.delivery_fee}


async def ask_zone_choice(zones: list[dict], tool_context: ToolContext) -> bool:
    """Le manda al cliente la lista de barrios posibles para que elija el
    suyo (lo atiende delivery/zone_choice.py, no la IA). True si salió."""
    business = Businesses.collection.get(tool_context.state.get("business_id"))
    customer = Customers.collection.get(tool_context.state.get("customer_id"))
    if business is None or customer is None:
        return False
    rows = [
        {"id": f"{CHOICE_PREFIX}{i}", "title": z.get("nombre", ""), "description": f"Domicilio {format_fee(z.get('tarifa'), '-')}"}
        for i, z in enumerate(zones)
    ]
    rows.append({"id": f"{CHOICE_PREFIX}{CHOICE_NONE}", "title": "Ninguno de estos"})
    wamid = await WhatsAppClient.for_business(business).send_interactive_list(
        customer.phone_number, "¿Cuál es tu barrio? Elígelo en la lista para calcular el domicilio 🛵", "Ver barrios", rows
    )
    return bool(wamid)


async def send_menu_image(tool_context: ToolContext) -> dict:
    """Vuelve a mandar al cliente la foto del menú de hoy (su carta o el
    menú del día) o, si el negocio usa catálogo, el link del catálogo. Úsala solo si el cliente la pide otra vez o no pudo verla:
    la primera vez del día ya se la manda el sistema solo.

    Returns:
        {"sent": True} si la imagen salió. {"sent": False} si hoy no hay
        foto o no se pudo enviar: entonces muéstrale el menú en texto con lo
        que devuelve get_active_menu.
    """
    business = Businesses.collection.get(tool_context.state.get("business_id"))
    customer = Customers.collection.get(tool_context.state.get("customer_id"))
    conversation = _conversation(tool_context)
    if business is None or customer is None or conversation is None:
        return {"sent": False}
    sent = await menu_image.send_menu_image(
        WhatsAppClient.for_business(business), business, customer, conversation, auto=False
    )
    return {"sent": sent}


async def request_delivery_quote(address: str, tool_context: ToolContext) -> dict:
    """Pide a un humano del equipo que verifique la dirección y cotice el
    domicilio cuando resolve_delivery_fee devolvió "zona_desconocida".

    El pedido queda en espera: no calcules el total ni lo confirmes hasta
    que resolve_delivery_fee devuelva "cotizado". Dile al cliente que espere
    un momento mientras terminamos de validar su pedido.

    Args:
        address: la dirección de entrega tal cual la dio el cliente.

    Returns:
        {"requested": True, "admin_notified": bool}
    """
    conversation = _conversation(tool_context)
    business = Businesses.collection.get(tool_context.state.get("business_id"))
    customer = Customers.collection.get(tool_context.state.get("customer_id"))
    if conversation is None or business is None or customer is None:
        return {"requested": False, "reason": "Conversación no encontrada."}
    notified = await quotes.request_quote(business, customer, conversation, address)
    return {"requested": True, "admin_notified": notified}


def save_order(order: dict, tool_context: ToolContext) -> dict:
    """Guarda el pedido ya confirmado por el cliente en Firestore.

    customer_id y business_id se toman del state de la sesión (no del
    modelo) para que el pedido siempre quede asociado al cliente correcto.

    El modelo `Orders` es una fila por ítem (un solo menu_id/quantity/total
    por documento, ver app.firestore_db.firebase_schemas.Orders y
    app.services.excel_report_service, que ya asumen esa forma), así que un
    pedido con varios ítems distintos genera un documento `Orders` por cada
    uno, todos con el mismo conversation_id para poder agruparlos después.

    Args:
        order: dict con el detalle del pedido confirmado, ej.
            {"items": [{"product": "menu_del_dia", "name": "...",
             "unit_price": 18500, "qty": 2, "elecciones": {"Sopa": "Ajiaco"},
             "proteina": None}], "total": 37000, "address": "...",
             "notes": "..."}
            (items es la lista resolved_items que devuelve
            validate_order_items -- product/qty/elecciones/proteina viajan
            tal cual, ver ese tool. Los ítems se vuelven a validar acá: el
            precio y el total siempre salen del menú de hoy, no de lo que
            pases en unit_price o total.)

    Returns:
        dict con el order_id del primer ítem guardado (referencia para
        print_order_ticket), la lista completa de
        order_ids creados y el estado guardado.
    """
    business_id = tool_context.state.get("business_id")
    customer_id = tool_context.state.get("customer_id")
    conversation = _conversation(tool_context)
    delivery_fee = _delivery_fee(conversation)
    if delivery_fee is None:
        return {
            "order_id": None,
            "order_ids": [],
            "status": "error",
            "reason": "Falta el valor del domicilio: resuélvelo antes de confirmar el pedido.",
        }
    conversation_id = conversation.conversation_id

    # Los ítems se vuelven a validar contra el menú vigente: el precio y la
    # cantidad que se cobran salen de acá, nunca de lo que mande el modelo
    # (un unit_price "ajustado" por prompt injection se ignora).
    validation = validate_order_items(order.get("items") or [], tool_context)
    items = validation["resolved_items"]
    if not validation["valid"] or not items:
        return {
            "order_id": None,
            "order_ids": [],
            "status": "error",
            "reason": "El pedido no pasó la validación contra el menú de hoy.",
            "errors": validation["errors"] or ["El pedido no tiene ítems."],
        }
    # Descuenta el stock limitado de forma atómica: si otro pedido se llevó
    # lo último entre la validación y acá, este no se guarda.
    stock_errors = stock.consume(business_id, [(i["name"], i["qty"]) for i in items])
    if stock_errors:
        return {
            "order_id": None,
            "order_ids": [],
            "status": "error",
            "reason": "Se acabó un producto del pedido mientras lo confirmabas.",
            "errors": stock_errors,
        }

    # Estado de entrega inicial: el pedido confirmado entra a cocina.
    status = OrderStatus.EN_COCINA.value
    address = order.get("address")
    now = datetime.now(timezone.utc)
    # Total confirmado con el cliente: ítems validados + domicilio
    # resuelto por el
    # código). Se guarda para validar una transferencia contra él sin
    # recalcular desde el menú.
    order_total = sum(i["unit_price"] * i["qty"] for i in items) + delivery_fee

    order_ids: list[str] = []
    for item in items:
        unit_price = item["unit_price"]
        qty = item["qty"]
        # Orders no tiene columnas propias para elecciones/proteina -- se
        # anexan a `notes` (texto libre) para no perder el detalle del
        # pedido, ej. "Proteína: Pollo | Sopa: Ajiaco".
        detail_parts = []
        if item.get("proteina"):
            detail_parts.append(f"Proteína: {item['proteina']}")
        for acomp, eleccion in (item.get("elecciones") or {}).items():
            detail_parts.append(f"{acomp.capitalize()}: {eleccion}")
        if item.get("notas"):
            detail_parts.append(f"Nota: {item['notas']}")
        notes = " | ".join(detail_parts + ([order.get("notes")] if order.get("notes") else []))

        saved = FireStoreCRUDService().create(
            Orders,
            business_id=business_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            menu_id=item.get("product") or item.get("item_id"),
            item_name=item.get("name"),
            status=status,
            quantity=qty,
            unit_price=unit_price,
            total=unit_price * qty,
            delivery_address=address,
            notes=notes,
            created_at=now,
            confirmed_at=now,
            # Solo el primer ítem (el documento de referencia del pedido).
            order_total=None if order_ids else order_total,
            delivery_zone=None if order_ids else conversation.delivery_zone,
            delivery_fee=None if order_ids else delivery_fee,
        )
        if not order_ids:
            # Consecutivo corto del día: va grande en la comanda y es lo que liquida el domiciliario.
            tracking.assign_daily_code(saved, now)
        order_ids.append(saved.order_id)

    return {
        "order_id": order_ids[0] if order_ids else None,
        "order_ids": order_ids,
        "status": status,
        "customer_id": customer_id,
        "business_id": business_id,
        "conversation_id": conversation_id,
        "total": order_total,
        "delivery_fee": delivery_fee,
        "address": address,
    }


async def print_order_ticket(order_id: str, ticket_text: str, tool_context: ToolContext) -> dict:
    """Envía la comanda del pedido ya guardado a la impresora térmica del negocio.

    `ticket_text` es SOLO la sección de qué pidió el cliente, en texto plano
    (sin markdown ni JSON), con una línea en blanco entre ítems:
      - Menú del día: una sola línea, ej. "2x Menú del día".
      - Otras opciones: "1x Otra opción:" y debajo, una línea por cosa: la
        proteína y cada acompañamiento elegido (ej. "Pollo a la plancha",
        "Arroz", "Ensalada").
      - Extras: "1x <nombre>".
    Si el cliente pidió alguna modificación o indicación especial (ej. "sin
    cebolla"), al final una línea en blanco, "Nota:" y debajo la indicación.
    Si no hay ninguna, no escribas "Nota:".

    No escribas precios, total, domicilio, dirección, datos del cliente ni
    método de pago: el sistema los agrega desde el pedido guardado.

    Llámala DESPUÉS de set_payment_method. Con efectivo la comanda sale a
    cocina de una vez; con transferencia queda retenida y sale sola cuando
    el comprobante se valida -- en ese caso devuelve printed=False y
    held=True, y eso NO es un error.

    El business_id sale del state de la sesión, no se le pide al modelo.

    Args:
        order_id: ID del pedido devuelto por save_order.
        ticket_text: la sección de ítems (y Nota, si aplica) de la comanda.

    Returns:
        dict con:
            - printed: bool, True si se envió correctamente a la impresora.
            - held: True si quedó esperando la validación de la transferencia.
            - reason: motivo si no se imprimió (o None si sí se imprimió).
            - sent_to_admin: True si la impresora falló y la comanda se le
              mandó al administrador para que no se pierda.
    """
    business_id = tool_context.state.get("business_id")

    order = Orders.collection.get(order_id)
    if order is None or order.business_id != business_id:
        return {"printed": False, "held": False, "print_job_id": None, "reason": "Pedido no encontrado."}
    # El método viaja a la impresora desde el pedido, no desde el modelo:
    # sin él no sale ninguna comanda.
    if not order.payment_method:
        return {
            "printed": False,
            "held": False,
            "print_job_id": None,
            "reason": "Falta el método de pago: pregúntaselo al cliente y llama a set_payment_method.",
        }

    print_job = FireStoreCRUDService().create(
        PrintJobs,
        business_id=business_id,
        order_id=order_id,
        content=build_ticket(
            Businesses.collection.get(business_id),
            Customers.collection.get(order.customer_id) if order.customer_id else None,
            order,
            ticket_text,
        ),
        status=PrintJobStatus.PENDING.value,
        attempts=0,
        created_at=datetime.now(timezone.utc),
    )

    held_reason = _HELD_REASONS.get(order.payment_status)
    if held_reason:
        return {
            "printed": False,
            "held": True,
            "print_job_id": print_job.print_job_id,
            "reason": held_reason,
        }

    success = await send_print_job(print_job, order.payment_method, order.cash_due)
    return {
        "printed": success,
        "held": False,
        "print_job_id": print_job.print_job_id,
        "reason": None if success else print_job.error,
        # Si la impresora falló, la comanda se le mandó al admin como respaldo.
        "sent_to_admin": print_job.admin_backup_sent_at is not None,
    }


# Mientras el pago esté en alguno de estos estados la comanda queda retenida;
# sale sola cuando se resuelve (ver print_jobs.release_held_ticket).
_HELD_REASONS = {
    PaymentStatus.PENDIENTE_COMPROBANTE.value: "La comanda espera a que se valide el comprobante de transferencia.",
    PaymentStatus.PENDIENTE_VALIDACION.value: "La comanda espera a que se valide el comprobante de transferencia.",
    PaymentStatus.PENDIENTE_AUTORIZACION.value: "La comanda espera a que la administración confirme si este pedido se cobra.",
}
_CUSTOMER_PAYMENT_METHODS = {PaymentMethod.EFECTIVO.value, PaymentMethod.TRANSFERENCIA.value}


def set_payment_method(order_id: str, method: str, tool_context: ToolContext) -> dict:
    """Guarda cómo va a pagar el cliente un pedido ya guardado.

    Llámala después de save_order, cuando el cliente elija. Solo hay dos
    métodos: "efectivo" o "transferencia".

    Args:
        order_id: ID del pedido devuelto por save_order.
        method: "efectivo" o "transferencia".

    Returns:
        dict con ok, payment_method, payment_status y amount_to_pay: el total
        confirmado del pedido. Con transferencia, amount_to_pay es el valor
        EXACTO que el cliente debe transferir -- díselo tal cual, no lo
        recalcules -- y pídele que envíe la foto del comprobante por este
        chat: el sistema lo valida solo y le avisa al cliente.
        Si algo falla: {"ok": False, "reason": "..."}.
    """
    business_id = tool_context.state.get("business_id")
    order = Orders.collection.get(order_id)
    if order is None or order.business_id != business_id:
        return {"ok": False, "reason": "Pedido no encontrado."}
    if order.payment_status == PaymentStatus.VALIDADO.value:
        return {"ok": False, "reason": "El pago de este pedido ya fue validado."}
    if order.payment_status == PaymentStatus.SIN_COBRO.value:
        return {"ok": False, "reason": "La administración confirmó que este pedido no se cobra."}

    method = (method or "").strip().lower()
    if method not in _CUSTOMER_PAYMENT_METHODS:
        return {"ok": False, "reason": "Método no válido: solo 'efectivo' o 'transferencia'."}

    order.payment_method = method
    order.payment_status = (
        PaymentStatus.POR_COBRAR.value
        if method == PaymentMethod.EFECTIVO.value
        else PaymentStatus.PENDIENTE_COMPROBANTE.value
    )
    FireStoreCRUDService().update(order)

    return {
        "ok": True,
        "order_id": order_id,
        "payment_method": order.payment_method,
        "payment_status": order.payment_status,
        "amount_to_pay": order.order_total,
    }


async def request_payment_agreement(order_id: str, motivo: str, tool_context: ToolContext) -> dict:
    """Pide a la administración que confirme si este pedido se cobra, cuando
    el cliente dice que tiene un acuerdo especial de pago: tiquetera, pago
    mensual, que ya lo pagó, o que la administradora le dijo que no le
    cobraran.

    NUNCA asumas que no se cobra: llama a esta tool y deja que la
    administración decida. Llámala en vez de set_payment_method, después de
    save_order; luego llama a print_order_ticket (la comanda queda en espera,
    held=True, no es un error) y dile al cliente que estás confirmando con la
    administración. No le pidas pago ni le digas que no se le cobra: el
    sistema le avisa la decisión. Si después ves en la conversación que la
    administración dijo que SÍ se cobra, sigue el cobro normal (efectivo o
    transferencia con set_payment_method).

    Args:
        order_id: ID del pedido devuelto por save_order.
        motivo: lo que dijo el cliente, con sus palabras (ej. "tengo
            tiquetera", "eso ya lo pagué").

    Returns:
        {"requested": True, "admin_notified": bool}, o {"requested": False,
        "reason": "..."}.
    """
    business_id = tool_context.state.get("business_id")
    customer_id = tool_context.state.get("customer_id")
    order = Orders.collection.get(order_id)
    if order is None or order.business_id != business_id or order.customer_id != customer_id:
        return {"requested": False, "reason": "Pedido no encontrado."}
    if order.payment_status in (PaymentStatus.VALIDADO.value, PaymentStatus.SIN_COBRO.value):
        return {"requested": False, "reason": "El pago de este pedido ya está resuelto."}

    business = Businesses.collection.get(business_id)
    customer = Customers.collection.get(customer_id)
    notified = await payment_agreement.request_authorization(business, customer, order, motivo)
    return {"requested": True, "admin_notified": notified}


async def report_delivery_update(tipo: str, mensaje_cliente: str, tool_context: ToolContext) -> dict:
    """Registra lo que dijo el cliente sobre un pedido que ya confirmó hoy.

    Args:
        tipo: "pregunta_por_pedido" (pregunta dónde está o cuándo llega: se
            avisa al restaurante), "ya_llego", "comprobante" o
            "queja_contenido".
        mensaje_cliente: el mensaje del cliente, tal cual lo escribió.

    Returns:
        {"ok": True, ...} si se registró; {"ok": False, "reason": ...} si el
        cliente no tiene pedidos hoy o el tipo no es válido (sigue atendiendo).
    """
    business = Businesses.collection.get(tool_context.state.get("business_id"))
    customer = Customers.collection.get(tool_context.state.get("customer_id"))
    if business is None or customer is None:
        return {"ok": False, "reason": "Cliente no encontrado."}
    return await tracking.report_customer_update(business, customer, tipo, mensaje_cliente)


async def responder_incidencia(accion: str, tool_context: ToolContext, detalle: str = "") -> dict:
    """Registra lo que el cliente decide sobre una novedad en la entrega de
    su pedido (el domiciliario está en su dirección y no ha podido
    entregarlo). Úsala SOLO cuando una nota del sistema te diga que hay una
    novedad de entrega y el cliente responda a eso.

    Args:
        accion: una de
            - "bajando": ya sale o baja a recibirlo.
            - "porteria": que lo dejen en la portería.
            - "otra_direccion": otra torre, apartamento, casa o punto de
              entrega (ponlo en `detalle`).
            - "otro_telefono": que llamen a otro número (ponlo en `detalle`).
            - "cancelar": ya no quiere el pedido.
            - "cambio_pago": quiere cambiar la forma de pago o lo que se cobra.
            - "otro": otra instrucción para el domiciliario (en `detalle`).
        detalle: la instrucción con sus palabras, si hace falta (ej. "torre
            3 apto 402").

    Returns:
        {"ok": True, "resultado": ..., "indicacion": ...}: sigue la indicación.
        {"ok": False, "reason": "sin_incidencia"} si ya no hay novedad activa:
        atiéndelo normal.
    """
    business = Businesses.collection.get(tool_context.state.get("business_id"))
    customer_id = tool_context.state.get("customer_id")
    incident = incidents.find_active_for_customer(business.bussiness_id, customer_id) if business else None
    if incident is None:
        return {"ok": False, "reason": "sin_incidencia"}
    return await incidents.resolve_from_customer(business, incident, accion, detalle)


# Únicos motivos por los que la IA puede pasar a un humano. "baja_confianza"
# ya no existe: la IA lo usaba para cualquier duda (ej. un pago mixto que el
# sistema ya maneja) y el cliente recibía "te atiende alguien" sin haberlo
# pedido (prueba real 2026-10-05).
_HANDOFF_MOTIVOS = {"solicitud_cliente", "precio_variable", "sin_datos"}


def request_human_handoff(
    motivo: str, resumen: str, tool_context: ToolContext, tema: Optional[str] = None
) -> dict:
    """Pasa la conversación a una persona del equipo. Es el ÚLTIMO recurso:
    el cliente recibe "En un momento te atiende alguien de nuestro equipo" y
    se avisa al chat de administración con una nota corta.

    Llámala SOLO en estos casos:
    - motivo="solicitud_cliente": el cliente dijo CLARAMENTE que quiere
      hablar con una persona (ej. "quiero hablar con alguien", "pásame con
      un asesor"). Que tenga una duda, esté confundido o se queje NO es
      pedir una persona: respóndele tú.
    - motivo="precio_variable" o "sin_datos": el cliente necesita un precio,
      horario o dato que NO está en ninguna tool ni en el menú, y sin él no
      puedes seguir. Nunca lo inventes.

    NUNCA la uses por pagos (comprobantes, efectivo restante, propinas,
    tiquetera o acuerdos: el sistema y sus tools ya los manejan), ni porque
    no estés seguro de qué responder: en ese caso pregúntale al cliente qué
    necesita y sigue atendiendo.

    Args:
        motivo: "solicitud_cliente", "precio_variable" o "sin_datos".
        resumen: UNA frase corta con lo que pasó, para la encargada (ej.
            "Reclamo por un pedido incompleto.", "Inconformidad con el cobro
            del pedido."). Sin copiar la conversación ni datos del cliente
            (nombre y teléfono los agrega el sistema).
        tema: opcional, de qué necesita hablar con la encargada, en pocas
            palabras (ej. "el pedido de hoy que llegó sin la sopa").

    Returns:
        {"requiere_humano": True, ...} si se registró; si el motivo no es
        válido, {"requiere_humano": False, "reason": ...}: sigue atendiendo
        tú al cliente.
    """
    if motivo not in _HANDOFF_MOTIVOS:
        return {
            "requiere_humano": False,
            "reason": "Ese motivo no amerita pasar a una persona: sigue atendiendo tú al cliente.",
        }
    tool_context.state["requiere_humano"] = True
    tool_context.state["motivo_handoff"] = motivo
    tool_context.state["resumen_handoff"] = resumen
    tool_context.state["tema_handoff"] = tema
    return {"requiere_humano": True, "motivo": motivo}

