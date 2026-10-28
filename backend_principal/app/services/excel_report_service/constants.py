"""Constantes y tipos compartidos por todo el paquete excel_report_service."""

from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo


ReportType = Literal[
    "daily_sales",
    "orders",
    "customers",
    "conversations",
    "lunch_stats",
    "dashboard",
]

# Antes existía un reporte "product_catalog" (antes "menu") que leía un
# catálogo de productos fijos (colección `MenuItems`) sin relación con el
# menú real. Esa colección y el menú del día ("daily_menus") se unificaron
# en una sola colección Firestore ("menu_items", modelo DailyMenus en
# firebase_schemas.py) porque tener dos fuentes de verdad para "el menú"
# generaba confusión real (2026-09-22, ver también HANDOFF.md/daily_menu).
# El reporte "product_catalog" se eliminó -- ya no hay un catálogo plano
# separado del menú del día. Los pedidos (Orders.menu_id) guardan
# literalmente "menu_del_dia" u "otras_opciones" (ver
# app/ai/ai_restaurant/user/tools/customer_tools.py::save_order), así que
# la resolución de nombre para reportes de ventas ya no necesita consultar
# Firestore: ver PRODUCT_ITEM_LABELS abajo.
#
# "plato_del_dia" sigue en la lista aunque ya no se genera: es el item_id
# viejo (antes del rename de 2026-09-23, ver vision_extractor.py) que
# quedó guardado en pedidos ya hechos. Sin esta entrada, los reportes de
# ventas de esos pedidos históricos mostrarían el ID crudo en vez del
# nombre legible.
PRODUCT_ITEM_LABELS: list[dict[str, str]] = [
    {"item_id": "menu_del_dia", "name": "Menú del día"},
    {"item_id": "plato_del_dia", "name": "Menú del día"},
    {"item_id": "otras_opciones", "name": "Otras opciones"},
]

DEFAULT_TZ = ZoneInfo("America/Bogota")

MONTH_NAMES_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]
WEEKDAY_NAMES_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

