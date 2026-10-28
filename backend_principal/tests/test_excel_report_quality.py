from app.firestore_db.firebase_schemas import Orders
from app.services.excel_report_service.dashboard_report import compute_dashboard_summary
from app.services.excel_report_service.firestore_repository import FirestoreReportRepository
from app.services.excel_report_service.models import ExcelReportSpec
from tests.conftest import seed_business


def test_compute_dashboard_summary_with_no_orders_is_empty(fake_store):
    business = seed_business(fake_store)
    repo = FirestoreReportRepository(connect=False)
    spec = ExcelReportSpec(business_id=business.bussiness_id, report_type="dashboard")

    assert compute_dashboard_summary(spec, repo) == {"report_type": "dashboard", "row_count": 0}


def test_compute_dashboard_summary_aggregates_orders(fake_store):
    # menu_id es literalmente "menu_del_dia"/"otras_opciones" (ver
    # customer_tools.py::save_order), resuelto vía PRODUCT_ITEM_LABELS.
    from datetime import datetime, timedelta

    from app.services.excel_report_service.constants import DEFAULT_TZ

    business = seed_business(fake_store)

    now = datetime.now(DEFAULT_TZ)
    for status in ("delivered", "delivered", "delivered", "cancelled"):
        order = Orders()
        order.business_id = business.bussiness_id
        order.menu_id = "menu_del_dia"
        order.quantity = 1
        order.unit_price = 25000
        order.total = 25000
        order.status = status
        order.created_at = now
        fake_store.seed(order)

    repo = FirestoreReportRepository(connect=False)
    spec = ExcelReportSpec(
        business_id=business.bussiness_id,
        report_type="dashboard",
        date_from=now - timedelta(hours=1),
        date_to=now + timedelta(hours=1),
    )

    summary = compute_dashboard_summary(spec, repo)

    assert summary["row_count"] == 4
    assert summary["total_revenue"] == 75000  # el cancelado no cuenta como venta
    assert summary["top_items"][0]["name"] == "Menú del día"
    assert {"status": "cancelled", "count": 1} in summary["by_status"]


def test_dashboard_names_legacy_plato_del_dia_as_menu_del_dia(fake_store):
    """Pedidos hechos antes del rename de 2026-09-23 guardaron el item_id
    viejo "plato_del_dia" -- PRODUCT_ITEM_LABELS lo mantiene mapeado a
    "Menú del día" a propósito (ver constants.py)."""
    business = seed_business(fake_store)
    order = Orders()
    order.business_id = business.bussiness_id
    order.menu_id = "plato_del_dia"
    order.quantity = 2
    order.unit_price = 18500
    order.status = "delivered"
    fake_store.seed(order)

    repo = FirestoreReportRepository(connect=False)
    spec = ExcelReportSpec(business_id=business.bussiness_id, report_type="dashboard")

    assert compute_dashboard_summary(spec, repo)["top_items"][0]["name"] == "Menú del día"
