"""Stock del día: lo que el admin dijo que se agotó o cuánto queda.

La IA del admin solo PIDE el cambio: el código ubica el producto entre los
de hoy y le manda botones Sí/No (request_change); se guarda solo con el
clic (apply_button). Los pedidos se validan y descuentan acá, nunca con lo
que diga la IA. Sin registro = sin límite. Vale solo para el día en que se
dijo: al otro día todo vuelve a estar disponible.
"""

import difflib
from collections import Counter
from typing import Optional

from app.firestore_db.firebase_schemas import DailyStock
from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia
from app.services.delivery.zones import normalize
from app.services.firestore_services import firestore_service

BUTTON_PREFIX = "stock:"


def _doc_id(business_id: str, fecha: str) -> str:
    return f"{business_id}_{fecha}"


def today_stock(business_id: str) -> dict:
    """{clave: {"nombre", "quedan"}} de hoy; vacío = nada limitado."""
    doc = DailyStock.collection.get(_doc_id(business_id, resolve_fecha_vigencia(None)))
    return dict((doc.items if doc else None) or {})


def stock_view(stock: dict) -> dict:
    """{"Pollo": 0, "Hamburguesa": 3} para mostrarle a la IA."""
    return {entry["nombre"]: int(entry["quedan"]) for entry in stock.values()}


def shortages(stock: dict, lines: list[tuple[str, int]]) -> list[str]:
    """Errores de las líneas (nombre, cantidad) que piden más de lo que queda,
    sumando las líneas del mismo producto."""
    need = Counter()
    for nombre, qty in lines:
        need[normalize(nombre)] += qty
    errors = []
    for key, qty in need.items():
        entry = stock.get(key)
        if entry is None or qty <= int(entry["quedan"]):
            continue
        quedan = int(entry["quedan"])
        errors.append(
            f"'{entry['nombre']}' se agotó por hoy."
            if quedan == 0
            else f"Solo quedan {quedan} de '{entry['nombre']}' hoy."
        )
    return errors


def consume(business_id: str, lines: list[tuple[str, int]]) -> list[str]:
    """Descuenta un pedido confirmado. Atómico: si dos clientes piden lo
    último a la vez, solo uno lo consigue. Devuelve errores (y no descuenta
    nada) si no alcanza."""
    doc_id = _doc_id(business_id, resolve_fecha_vigencia(None))

    def _consume(t):
        doc = DailyStock.collection.get(doc_id, transaction=t)
        stock = dict((doc.items if doc else None) or {})
        errors = shortages(stock, lines)
        limited = [(normalize(n), q) for n, q in lines if normalize(n) in stock]
        if errors or not limited:
            return errors
        for key, qty in limited:
            stock[key] = {**stock[key], "quedan": int(stock[key]["quedan"]) - qty}
        doc.items = stock
        doc.save(transaction=t)
        return []

    return firestore_service.run_in_transaction(_consume)


def set_stock(business_id: str, nombre: str, cantidad: Optional[int]) -> None:
    """cantidad=None quita el límite."""
    fecha = resolve_fecha_vigencia(None)
    doc_id = _doc_id(business_id, fecha)

    def _set(t):
        doc = DailyStock.collection.get(doc_id, transaction=t) or DailyStock(
            stock_id=doc_id, business_id=business_id, fecha=fecha, items={}
        )
        stock = dict(doc.items or {})
        if cantidad is None:
            stock.pop(normalize(nombre), None)
        else:
            stock[normalize(nombre)] = {"nombre": nombre, "quedan": cantidad}
        doc.items = stock
        doc.save(transaction=t)

    firestore_service.run_in_transaction(_set)


def menu_products(menu: dict) -> dict[str, str]:
    """{texto normalizado: nombre} de lo que se puede pedir hoy, a partir de
    lo que devuelve get_active_menu. "menu del dia" apunta al plato del día."""
    names = [extra["name"] for extra in menu.get("extras_fijos") or []]
    if menu.get("menu_available"):
        names += menu["otras_opciones"]["proteinas"]
        names.append(menu["menu_del_dia"]["nombre"])
    products = {normalize(n): n for n in names if n}
    if menu.get("menu_available") and menu["menu_del_dia"]["nombre"]:
        products["menu del dia"] = menu["menu_del_dia"]["nombre"]
    return products


def match_product(text: str, products: dict[str, str]) -> list[str]:
    """Los nombres de hoy que corresponden a lo que escribió el admin:
    exacto, contenido ("hamburguesa" -> "Hamburguesa doble") o parecido."""
    query = normalize(text)
    if not query:
        return []
    if query in products:
        return [products[query]]
    padded = f" {query} "
    keys = [k for k in products if f" {k} " in padded or padded in f" {k} "]
    keys = keys or difflib.get_close_matches(query, list(products), n=3, cutoff=0.8)
    return sorted({products[k] for k in keys})


def confirmation(nombre: str, cantidad: Optional[int]) -> tuple[str, list[dict]]:
    """Texto y botones Sí/No del cambio. El id lleva la fecha: un clic en un
    botón de otro día no toca el stock de hoy."""
    if cantidad is None:
        body = f"¿*{nombre}* vuelve a estar disponible sin límite hoy?"
    elif cantidad == 0:
        body = f"¿Marco *{nombre}* como agotado por hoy?"
    else:
        body = f"¿Confirmas que hoy quedan *{cantidad}* de *{nombre}*?"
    qty = "-" if cantidad is None else str(cantidad)
    fecha = resolve_fecha_vigencia(None)
    return body, [
        {"id": f"{BUTTON_PREFIX}{fecha}:{qty}:{nombre}", "title": "✅ Sí"},
        {"id": f"{BUTTON_PREFIX}no", "title": "❌ No"},
    ]


def apply_button(business_id: str, button_id: str) -> str:
    """Aplica el clic del admin y devuelve qué contestarle."""
    rest = button_id[len(BUTTON_PREFIX):]
    if rest == "no":
        return "Listo, no cambié nada."
    fecha, _, rest = rest.partition(":")
    qty, _, nombre = rest.partition(":")
    if fecha != resolve_fecha_vigencia(None):
        return "Ese aviso era de otro día, no cambié nada. Dime de nuevo qué quedó."
    try:
        cantidad = None if qty == "-" else int(qty)
    except ValueError:
        return "No entendí ese botón, no cambié nada."
    set_stock(business_id, nombre, cantidad)
    if cantidad is None:
        return f"Listo: *{nombre}* vuelve a estar disponible."
    if cantidad == 0:
        return f"Listo: *{nombre}* quedó agotado por hoy. No se lo ofrezco a nadie más."
    return f"Listo: quedan *{cantidad}* de *{nombre}*. Lo voy descontando con cada pedido."

