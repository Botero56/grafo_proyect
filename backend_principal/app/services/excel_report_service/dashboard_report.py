"""Reporte "dashboard": inyecta los pedidos reales en una plantilla de Excel.

El diseño (gráficos, tablas dinámicas, fórmulas, KPIs) vive en
`Plantillas Excel/Plantilla_Dashboard.xlsx`, hecha a mano en Excel. Python
NO dibuja nada: abre la plantilla, reemplaza las filas de la tabla `datos`
(hoja `Datos`) por los pedidos del rango pedido, ajusta el rango de la tabla
y marca el libro para que Excel recalcule fórmulas y refresque las tablas
dinámicas al abrirlo.

Una fila = un documento `Orders`, que ya es un ítem (ver
customer_tools.save_order): así Cantidad/Valor se pueden sumar sin
duplicar. Los datos del pedido completo (pago, zona, domicilio) solo los
lleva el primer ítem y quedan vacíos en los demás, por la misma razón.

La plantilla manda: se llenan solo las columnas cuyo encabezado aparece en
la tabla `datos`, en el orden de la plantilla. Una columna de la plantilla
que no esté en DASHBOARD_COLUMNS es un error explícito, nunca una columna
vacía silenciosa.
"""

from __future__ import annotations

import logging
from copy import copy
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries

from .constants import PRODUCT_ITEM_LABELS
from .firestore_repository import FirestoreReportRepository, as_datetime, resolve_order_item
from .models import ExcelReportSpec
from .paths import default_output_path

logger = logging.getLogger(__name__)

DASHBOARD_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[3] / "Plantillas Excel" / "Plantilla_Dashboard.xlsx"
)
DASHBOARD_SHEET = "Datos"
DASHBOARD_TABLE = "datos"

# Columnas disponibles para la tabla `datos`, todas sacadas de campos reales
# de Orders (o de Customers, cruzado por customer_id). "Periodo" es la única
# calculada: la usan las tablas dinámicas para agrupar por mes.
DASHBOARD_COLUMNS = [
    "Fecha",
    "Periodo",
    "Cliente",
    "Teléfono",
    "Producto",
    "Detalle",
    "Cantidad",
    "Precio unitario",
    "Valor",
    "Estado",
    "Método de pago",
    "Estado de pago",
    "Zona",
    "Domicilio",
]


def build_dashboard_dataframe(
    spec: ExcelReportSpec,
    orders: list[dict[str, Any]],
    customers: list[dict[str, Any]],
) -> pd.DataFrame:
    menu_by_id = {item["item_id"]: item for item in PRODUCT_ITEM_LABELS}
    customers_by_id = {c.get("customer_id"): c for c in customers}

    rows = []
    for order in orders:
        product, quantity, unit_price = resolve_order_item(order, menu_by_id)
        total = order.get("total")
        customer = customers_by_id.get(order.get("customer_id"), {})
        rows.append(
            {
                "Fecha": _local_naive(order.get("created_at"), spec),
                "Cliente": customer.get("name") or "Cliente sin nombre",
                "Teléfono": customer.get("phone_number"),
                "Producto": product,
                "Detalle": order.get("notes"),
                "Cantidad": quantity,
                "Precio unitario": unit_price,
                "Valor": float(total) if total is not None else quantity * unit_price,
                "Estado": order.get("status"),
                "Método de pago": order.get("payment_method"),
                "Estado de pago": order.get("payment_status"),
                "Zona": order.get("delivery_zone"),
                "Domicilio": order.get("delivery_fee"),
            }
        )

    df = pd.DataFrame(rows, columns=[c for c in DASHBOARD_COLUMNS if c != "Periodo"])
    if df.empty:
        return df.reindex(columns=DASHBOARD_COLUMNS)

    df["Fecha"] = pd.to_datetime(df["Fecha"])
    df.insert(1, "Periodo", df["Fecha"].dt.strftime("%Y-%m"))
    return df.sort_values("Fecha", kind="stable").reset_index(drop=True)


def _local_naive(value: Any, spec: ExcelReportSpec):
    """Fecha en la hora del negocio y sin zona: Excel no guarda zona
    horaria, y una fecha con tzinfo hace fallar a openpyxl."""
    local = as_datetime(value, ZoneInfo(spec.timezone))
    return local.replace(tzinfo=None) if local else None


def compute_dashboard_data(
    spec: ExcelReportSpec, repo: FirestoreReportRepository
) -> pd.DataFrame:
    # El business_id va en la consulta a Firestore (fetch_orders); el rango
    # de fechas se aplica después, ya solo sobre los pedidos de ese negocio.
    orders = repo.fetch_orders(spec)
    customers = repo.fetch_customers(spec) if orders else []
    return build_dashboard_dataframe(spec, orders, customers)


def summarize_dashboard(df: pd.DataFrame) -> dict[str, Any]:
    """Resumen JSON-liviano de lo que se inyectó en la plantilla, para que
    el admin_agent responda preguntas de seguimiento sin reabrir el
    archivo (ver generate_excel_report en admin_tools.py). Solo totales
    directos de las filas: los indicadores del dashboard los calcula Excel."""
    if df.empty:
        return {"report_type": "dashboard", "row_count": 0}

    valid = df[df["Estado"].fillna("").str.strip().str.lower() != "cancelled"]
    top_items = (
        valid.groupby("Producto")[["Cantidad", "Valor"]]
        .sum()
        .sort_values("Cantidad", ascending=False)
        .head(5)
    )
    by_status = df.groupby(df["Estado"].fillna("sin_estado")).size()
    return {
        "report_type": "dashboard",
        "row_count": int(len(df)),
        "date_first": df["Fecha"].min().isoformat(),
        "date_last": df["Fecha"].max().isoformat(),
        "total_revenue": float(valid["Valor"].sum()),
        "total_items_sold": int(valid["Cantidad"].sum()),
        "top_items": [
            {"name": name, "quantity": int(r["Cantidad"]), "revenue": float(r["Valor"])}
            for name, r in top_items.iterrows()
        ],
        "by_status": [{"status": s, "count": int(n)} for s, n in by_status.items()],
    }


def compute_dashboard_summary(spec: ExcelReportSpec, repo: FirestoreReportRepository) -> dict[str, Any]:
    return summarize_dashboard(compute_dashboard_data(spec, repo))


def generate_dashboard_report(spec: ExcelReportSpec, repo: FirestoreReportRepository) -> Path:
    output_path = Path(spec.output_path or default_output_path(spec.business_id, spec.report_type))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fill_dashboard_template(compute_dashboard_data(spec, repo), DASHBOARD_TEMPLATE_PATH, output_path)
    logger.info("Dashboard generado desde plantilla: %s", output_path)
    return output_path


def fill_dashboard_template(
    df: pd.DataFrame,
    template: Path,
    output: Path,
    sheet: str = DASHBOARD_SHEET,
    table: str = DASHBOARD_TABLE,
) -> None:
    """Reemplaza las filas de la tabla `table` por `df` y guarda una copia.

    Todo lo demás de la plantilla (hojas, estilos, gráficos, tablas
    dinámicas, fórmulas) se conserva: solo se tocan las filas de datos de
    esa tabla y su rango."""
    if df.empty:
        raise ValueError("Sin datos para el dashboard")

    workbook = load_workbook(template)
    worksheet = workbook[sheet]
    excel_table = worksheet.tables[table]

    min_col, header_row, max_col, _ = range_boundaries(excel_table.ref)
    headers = [worksheet.cell(row=header_row, column=c).value for c in range(min_col, max_col + 1)]
    missing = [h for h in headers if h not in df.columns]
    if missing:
        raise ValueError(
            f"La tabla '{table}' de la plantilla tiene columnas que el reporte no "
            f"produce: {missing}. Disponibles: {list(df.columns)}"
        )

    # El formato de cada columna (número, fecha, moneda) lo define la
    # primera fila de datos de la plantilla; se copia a todas las filas
    # nuevas antes de borrar las viejas.
    first_data_row = header_row + 1
    column_styles = [
        copy(worksheet.cell(row=first_data_row, column=c)._style)
        for c in range(min_col, max_col + 1)
    ]

    rows = df[headers].astype(object).where(df[headers].notna(), None).values.tolist()

    worksheet.delete_rows(first_data_row, max(worksheet.max_row - header_row, 1))
    for r, row in enumerate(rows, start=first_data_row):
        for offset, value in enumerate(row):
            # Estilo antes que valor: así openpyxl solo pone un formato de
            # fecha cuando la plantilla no trae uno propio para esa columna.
            cell = worksheet.cell(row=r, column=min_col + offset)
            cell._style = copy(column_styles[offset])
            cell.value = value

    ref = (
        f"{get_column_letter(min_col)}{header_row}:"
        f"{get_column_letter(max_col)}{header_row + len(rows)}"
    )
    excel_table.ref = ref
    if excel_table.autoFilter is not None:
        excel_table.autoFilter.ref = ref

    workbook.calculation.fullCalcOnLoad = True
    for ws in workbook.worksheets:
        for pivot in ws._pivots:
            pivot.cache.refreshOnLoad = True

    workbook.save(output)
