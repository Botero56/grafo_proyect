from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.ai.ai_restaurant.admin.admin_tools import generate_excel_report, get_customer_chat_reference
from app.firestore_db.firebase_schemas import Orders
from tests.conftest import FakeToolContext, seed_business, seed_customer


def test_generate_excel_report_empty_when_no_orders(fake_store):
    business = seed_business(fake_store)
    ctx = FakeToolContext(business.bussiness_id)

    result = generate_excel_report(ctx, date_from="2026-09-01", date_to="2026-10-01")

    assert result["status"] == "empty"
    assert result["file_name"] is None
    assert "pending_document" not in ctx.state


def test_generate_excel_report_with_orders_returns_summary_and_sets_pending_document(fake_store):
    business = seed_business(fake_store)
    customer = seed_customer(fake_store, business, "573001111111", name="Camilo")
    order = Orders()
    order.business_id = business.bussiness_id
    order.customer_id = customer.customer_id
    order.menu_id = "menu_del_dia"
    order.quantity, order.unit_price, order.total = 1, 18500, 18500
    order.status = "confirmed"
    order.created_at = datetime(2026, 9, 15, 17, 0, tzinfo=timezone.utc)
    fake_store.seed(order)

    ctx = FakeToolContext(business.bussiness_id)
    fake_path = Path("/tmp/fake_report.xlsx")
    with patch(
        "app.ai.ai_restaurant.admin.admin_tools.ExcelReportService.generate_report",
        return_value=fake_path,
    ) as generate:
        result = generate_excel_report(ctx, date_from="2026-09-01", date_to="2026-10-01")

    assert generate.call_args.args[0].report_type == "dashboard"
    assert result["status"] == "ok"
    assert result["summary"]["row_count"] == 1
    assert ctx.state["pending_document"] == {"path": str(fake_path), "filename": "fake_report.xlsx"}


def test_generate_excel_report_requires_dates(fake_store):
    business = seed_business(fake_store)

    result = generate_excel_report(FakeToolContext(business.bussiness_id))

    assert result["status"] == "missing_dates"


def test_get_customer_chat_reference_found(fake_store):
    business = seed_business(fake_store)
    seed_customer(fake_store, business, "573001111111", name="Camilo")

    result = get_customer_chat_reference(FakeToolContext(business.bussiness_id), "573001111111")

    assert result == {
        "found": True,
        "customer_name": "Camilo",
        "phone_number": "573001111111",
        "whatsapp_link": "https://wa.me/573001111111",
    }


def test_get_customer_chat_reference_not_found(fake_store):
    business = seed_business(fake_store)

    result = get_customer_chat_reference(FakeToolContext(business.bussiness_id), "573009999999")

    assert result == {"found": False}


def test_chat_reference_for_a_username_customer_never_invents_a_link(fake_store):
    """Prueba real 2026-10-20: la IA le dio al admin https://wa.me/CO.178..., que no abre nada."""
    business = seed_business(fake_store)
    seed_customer(fake_store, business, "CO.1780159176423859", name="Sebastián")
    ctx = FakeToolContext(business.bussiness_id)

    assert get_customer_chat_reference(ctx, "CO.1780159176423859")["whatsapp_link"] is None

    seed_customer(fake_store, business, "CO.999", name="Luz", contact_phone="573002223344")
    assert get_customer_chat_reference(ctx, "CO.999")["whatsapp_link"] == "https://wa.me/573002223344"


def test_username_customer_is_found_and_shown_by_the_phone_they_gave(fake_store):
    """Prueba real 2026-10-21: "¿cuál es el número de Sebastián G?" respondía su código de usuario."""
    from app.ai.ai_restaurant.admin.admin_tools import find_orders_by_customer

    business = seed_business(fake_store)
    seed_customer(fake_store, business, "CO.1780159176423859", name="Sebastián G", contact_phone="573054283889")
    ctx = FakeToolContext(business.bussiness_id)

    for asked in ("3054283889", "573054283889", "CO.1780159176423859"):
        result = find_orders_by_customer(ctx, asked)
        assert result["found_customer"] and result["phone_number"] == "573054283889"
    assert get_customer_chat_reference(ctx, "3054283889")["whatsapp_link"] == "https://wa.me/573054283889"


def test_admin_finds_customers_by_name(fake_store):
    """Prueba real 2026-10-21: '¿cuál es el número de Sebastián G?' se respondía de memoria con el código."""
    from app.ai.ai_restaurant.admin.admin_tools import find_orders_by_customer

    business = seed_business(fake_store)
    seed_customer(fake_store, business, "CO.1780159176423859", name="Sebastián G", contact_phone="573054283889")
    seed_customer(fake_store, business, "573001111111", name="Ana")
    seed_customer(fake_store, business, "573002222222", name="Ana María")
    seed_customer(fake_store, business, "CO.555", name="Luz")
    ctx = FakeToolContext(business.bussiness_id)

    for asked in ("sebastian g", "Sebastián", "seba"):
        assert find_orders_by_customer(ctx, asked)["phone_number"] == "573054283889"
    assert find_orders_by_customer(ctx, "ana")["phone_number"] == "573001111111"  # el nombre exacto gana
    ambiguous = find_orders_by_customer(ctx, "an")
    assert ambiguous["reason"] == "varios_clientes" and len(ambiguous["opciones"]) == 2
    assert find_orders_by_customer(ctx, "Pedro") == {"found_customer": False, "orders": []}
    # Escribe con usuario y no ha dado número: nunca se muestra el código como teléfono.
    assert find_orders_by_customer(ctx, "Luz")["phone_number"] is None
