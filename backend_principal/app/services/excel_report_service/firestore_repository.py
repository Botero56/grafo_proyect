"""Acceso a Firestore para el servicio de reportes Excel.

Aísla toda la conexión y las consultas (`fireo`/`google.cloud.firestore`) del
resto del paquete, que solo trabaja con `dict`s ya normalizados.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.firestore_db.firebase_schemas import Customers, Orders
from app.services.firestore_services.firestore_service import ensure_firestore_connection
from app.services.delivery.zones import normalize
from app.services.phones import contact_phone, is_username_id, normalize_phone

# Menos dígitos que esto no es un teléfono: se busca como nombre.
_MIN_PHONE_DIGITS = 7

from .models import ExcelReportSpec

# Campos que se copian del modelo de Firestore al dict plano usado por los
# generadores de reportes, cuando `to_dict()` no está disponible o no los
# incluye.
_MODEL_FIELDS = (
    "order_id",
    "conversation_id",
    "menu_id",
    "item_name",
    "status",
    "quantity",
    "unit_price",
    "total",
    "delivery_address",
    "notes",
    "created_at",
    "confirmed_at",
    "dispatched_at",
    "delivered_at",
    "customer_id",
    "name",
    "phone_number",
    "whatsapp_user_id",
    "address",
    "last_interaction_at",
    "updated_at",
    "channel",
    "started_at",
    "last_message_at",
    "closed_at",
    "active_order_id",
)


class FirestoreReportRepository:
    """Consulta Firestore y normaliza los modelos a `dict`s planos."""

    def __init__(self, credentials: Any = None, connect: bool = True) -> None:
        if connect:
            self.connect(credentials)

    def connect(self, credentials: Any = None) -> None:
        if credentials is None:
            ensure_firestore_connection()
        else:
            ensure_firestore_connection(credentials)

    def fetch_orders(self, spec: ExcelReportSpec) -> list[dict[str, Any]]:
        return self.filter_order_dicts(self.fetch_all_orders_unfiltered(spec.business_id), spec)

    def fetch_all_orders_unfiltered(self, business_id: str) -> list[dict[str, Any]]:
        orders = list(Orders.collection.filter(business_id=business_id).fetch())
        return [self.model_to_dict(order) for order in orders]

    def fetch_customers(self, spec: ExcelReportSpec) -> list[dict[str, Any]]:
        customers = list(
            Customers.collection.filter(business_id=spec.business_id).fetch()
        )
        return [self.customer_to_dict(item) for item in customers]

    def fetch_customer_by_phone(
        self, business_id: str, phone_number: str
    ) -> Optional[dict[str, Any]]:
        """Por su número de WhatsApp, su código de usuario o, si escribe con
        usuario, por el celular que nos dio."""
        key = phone_number if is_username_id(phone_number) else normalize_phone(phone_number)
        customer = Customers.collection.filter(business_id=business_id, phone_number=key).get()
        if customer is None and not is_username_id(key):
            customer = Customers.collection.filter(business_id=business_id, contact_phone=key).get()
        return self.customer_to_dict(customer) if customer else None

    def find_customers(self, business_id: str, who: str) -> list[dict[str, Any]]:
        """El cliente al que se refiere el admin: por teléfono, código de
        usuario o celular que dio; si no, por nombre ("seba" -> "Sebastián
        G"). Varios si el nombre es ambiguo, salvo que uno sea exacto."""
        who = (who or "").strip()
        if is_username_id(who) or len(normalize_phone(who)) >= _MIN_PHONE_DIGITS:
            found = self.fetch_customer_by_phone(business_id, who)
            return [found] if found else []
        key = normalize(who)
        if not key:
            return []
        # ponytail: recorre los clientes del negocio; índice por nombre si pasan de miles.
        customers = Customers.collection.filter(business_id=business_id).fetch()
        matches = [c for c in customers if f" {key}" in f" {normalize(c.name)}"]
        exact = [c for c in matches if normalize(c.name) == key]
        return [self.customer_to_dict(c) for c in (exact if len(exact) == 1 else matches)]

    def customer_to_dict(self, customer: Customers) -> dict[str, Any]:
        """Para personas (admin, Excel): `phone_number` es su celular real
        (el que dio, si escribe con usuario de WhatsApp); el código de
        usuario queda en `whatsapp_user_id`. None si escribe con usuario y
        todavía no nos dio su número."""
        data = self.model_to_dict(customer)
        data["phone_number"] = contact_phone(customer)
        return data

    def fetch_orders_by_customer(
        self, business_id: str, customer_id: str
    ) -> list[dict[str, Any]]:
        # customer_id en Orders solo existe para pedidos guardados después de
        # que se agregó el campo (ver firebase_schemas.Orders): pedidos más
        # viejos no aparecerán acá aunque sean del mismo cliente.
        orders = Orders.collection.filter(
            business_id=business_id, customer_id=customer_id
        ).fetch()
        return [self.model_to_dict(order) for order in orders]

    def filter_order_dicts(
        self,
        records: list[dict[str, Any]],
        spec: ExcelReportSpec,
    ) -> list[dict[str, Any]]:
        tz = ZoneInfo(spec.timezone)
        date_from = as_datetime(spec.date_from, tz)
        date_to = as_datetime(spec.date_to, tz)
        filtered = []
        for record in records:
            created = as_datetime(record.get("created_at"), tz)
            if date_from or date_to:
                if created is None:
                    continue
                if date_from and created < date_from:
                    continue
                if date_to and created >= date_to:
                    continue
            if spec.order_status and record.get("status") != spec.order_status:
                continue
            filtered.append(record)
        return filtered

    def model_to_dict(self, model: Any) -> dict[str, Any]:
        data: dict[str, Any] = {}
        to_dict = getattr(model, "to_dict", None)
        if callable(to_dict):
            try:
                data.update(to_dict() or {})
            except Exception:
                pass
        for field_name in _MODEL_FIELDS:
            if hasattr(model, field_name):
                data[field_name] = getattr(model, field_name)
        return data


def as_datetime(value: Any, tz: ZoneInfo) -> Optional[datetime]:
    """Fecha en `tz`; una fecha sin zona se interpreta directamente en `tz`.
    None si no es una fecha."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)
    return None


def resolve_order_item(
    order: dict[str, Any], menu_by_id: dict[str, dict[str, Any]]
) -> tuple[str, int, float]:
    """(nombre legible, cantidad, precio unitario) de un pedido crudo. El
    precio sale del pedido y, si no lo trae, del ítem del menú."""
    menu_id = order.get("menu_id")
    menu_item = menu_by_id.get(menu_id, {})
    name = menu_item.get("name") or order.get("item_name") or menu_id or "Sin nombre"

    quantity = int(order.get("quantity") or 0)
    unit_price = order.get("unit_price")
    if unit_price is None:
        unit_price = menu_item.get("price") or 0
    return name, quantity, float(unit_price)
