"""Sincroniza el catálogo de WhatsApp Business (Commerce Manager) de un
negocio con sus FixedMenuItems.

Meta no avisa por webhook cuando cambia un producto del catálogo, así que
no se consulta solo: el admin avisa desde el chat ("actualicé el
catálogo") y el admin_agent llama la tool sync_catalog, que usa esto.

Cada producto del catálogo queda como un FixedMenuItems con
`catalog_retailer_id`; los productos fijos creados a mano (sin ese campo)
nunca se tocan. Precio, nombre y disponibilidad salen de Meta por código,
nunca de la IA.
"""

import logging
import re
from typing import Optional

import httpx

from app.firestore_db.firebase_schemas import Businesses, FixedMenuItems
from app.schemas.enum_schemas import PriceType
from app.services.daily_menu.menu_diff import format_cop
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient

logger = logging.getLogger("whatsapp-webhook")

_FIELDS = "retailer_id,name,description,price,availability,visibility"
_UNAVAILABLE = {"out of stock", "discontinued"}
# retailer_id de los productos que vienen de una foto de carta (ver
# orchestrator._apply_carta), para que la carta y el catálogo de Meta no se
# borren uno al otro.
CARTA_PREFIX = "carta:"


def parse_price(raw: Optional[str]) -> Optional[float]:
    """Meta entrega el precio formateado ("$25.000", "COP25,000.00",
    "$ 25.000,00"). Se descartan los centavos y los separadores de miles;
    None si no queda ningún número (el producto queda "no_legible")."""
    number = re.sub(r"[^\d.,]", "", raw or "")
    number = re.sub(r"[.,]\d{2}$", "", number)
    number = re.sub(r"[.,]", "", number)
    return float(number) if number else None


async def fetch_catalog_products(client: WhatsAppClient, catalog_id: str) -> Optional[list[dict]]:
    """Todos los productos del catálogo (recorre la paginación), o None si
    Meta no respondió bien -- en ese caso no se debe tocar Firestore."""
    products: list[dict] = []
    url: Optional[str] = f"{catalog_id}/products"
    params: Optional[dict] = {"fields": _FIELDS, "limit": 100}
    while url:
        try:
            response = await client.get(url, params=params)
        except httpx.HTTPError:
            logger.exception("Catálogo %s: error de red leyendo productos", catalog_id)
            return None
        if response.status_code != 200:
            logger.error(
                "Catálogo %s: Meta respondió http=%s: %s",
                catalog_id, response.status_code, response.text[:300],
            )
            return None
        body = response.json()
        products += body.get("data") or []
        url = (body.get("paging") or {}).get("next")
        params = None  # la url "next" ya trae los parámetros
    return products


def _changes(item: FixedMenuItems, fields: dict) -> list[str]:
    changes = []
    if item.nombre != fields["nombre"]:
        changes.append(f'nombre "{item.nombre}" → "{fields["nombre"]}"')
    if item.precio != fields["precio"]:
        changes.append(
            f"precio {format_cop(item.precio, 'sin precio')} → {format_cop(fields['precio'], 'sin precio')}"
        )
    if item.is_active != fields["is_active"]:
        changes.append("ahora disponible" if fields["is_active"] else "ahora agotado")
    if (item.descripcion or None) != fields["descripcion"]:
        changes.append("descripción")
    return changes


def apply_catalog(business_id: str, products: list[dict], carta: bool = False, prune: bool = True) -> dict:
    """Deja los FixedMenuItems del catálogo (o de la carta, con carta=True)
    igual a `products` y devuelve qué cambió, para mostrárselo al admin.
    prune=False solo agrega/actualiza: lo que ya estaba y no viene se queda
    (juntar una carta nueva con la anterior)."""
    existing = {
        item.catalog_retailer_id: item
        for item in TenantScopedQuery.list_for_business(FixedMenuItems, business_id)
        if item.catalog_retailer_id
        and item.catalog_retailer_id.startswith(CARTA_PREFIX) == carta
    }
    crud = FireStoreCRUDService()
    created: list[str] = []
    updated: list[str] = []
    seen: set[str] = set()

    for product in products:
        retailer_id = product.get("retailer_id")
        if not retailer_id or retailer_id in seen:
            continue
        seen.add(retailer_id)

        precio = parse_price(product.get("price"))
        fields = {
            "nombre": product.get("name") or retailer_id,
            "descripcion": product.get("description") or None,
            "precio": precio,
            "tipo_precio": PriceType.FIJO.value if precio is not None else PriceType.NO_LEGIBLE.value,
            "is_active": product.get("availability") not in _UNAVAILABLE
            and product.get("visibility") != "hidden",
            "categoria": product.get("categoria") or "Catálogo",
        }

        item = existing.get(retailer_id)
        if item is None:
            crud.create(
                FixedMenuItems,
                business_id=business_id,
                catalog_retailer_id=retailer_id,
                **fields,
            )
            created.append(fields["nombre"])
            continue

        changes = _changes(item, fields)
        if changes:
            for key, value in fields.items():
                setattr(item, key, value)
            crud.update(item)
            updated.append(f"{fields['nombre']}: {', '.join(changes)}")

    deleted = []
    for retailer_id, item in existing.items():
        if prune and retailer_id not in seen:
            crud.delete(item)
            deleted.append(item.nombre)

    return {"creados": created, "modificados": updated, "eliminados": deleted}


async def sync_catalog(business: Businesses, catalog_id: str) -> Optional[dict]:
    """Lee el catálogo de Meta y lo aplica. None si no se pudo leer (no se
    toca nada en Firestore)."""
    products = await fetch_catalog_products(WhatsAppClient.for_business(business), catalog_id)
    if products is None:
        return None
    return apply_catalog(business.bussiness_id, products)
