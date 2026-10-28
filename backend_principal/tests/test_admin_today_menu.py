from app.ai.ai_restaurant.admin.admin_tools import get_today_menu, mark_closed_today, reuse_last_menu
from app.firestore_db.firebase_schemas import DailyMenus
from app.schemas.enum_schemas import DailyMenuStatus
from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia
from app.services.daily_menu.daily_menu_service import DailyMenuService
from tests.conftest import FakeToolContext, make_menu, seed_business, seed_customer


def test_no_active_menu_reports_unavailable(fake_store):
    business = seed_business(fake_store)

    result = get_today_menu(FakeToolContext(business.bussiness_id))

    assert result["menu_available"] is False
    assert result["carta_available"] is False
    assert result["text"] is None


def test_closed_today_reports_closed_instead_of_unavailable(fake_store):
    business = seed_business(fake_store)
    DailyMenuService().mark_closed_today(
        business.bussiness_id, resolve_fecha_vigencia(None), None, "cerrado por remodelación"
    )

    result = get_today_menu(FakeToolContext(business.bussiness_id))

    assert result == {
        "menu_available": False,
        "closed_today": True,
        "closed_reason": "cerrado por remodelación",
        "text": None,
    }


def test_mark_closed_today_tool_stores_admin_phone_from_session(fake_store):
    business = seed_business(fake_store)
    admin = seed_customer(fake_store, business, "573001111111")

    result = mark_closed_today(
        FakeToolContext(business.bussiness_id, customer_id=admin.customer_id), motivo="vacaciones"
    )

    assert result == {"closed": True, "date": resolve_fecha_vigencia(None), "motivo": "vacaciones"}
    record = DailyMenuService().get_closed_for_date(business.bussiness_id, resolve_fecha_vigencia(None))
    assert record.admin_phone == "573001111111"
    assert record.caption == "vacaciones"


def test_active_menu_returns_formatted_text(fake_store):
    business = seed_business(fake_store)
    today = resolve_fecha_vigencia(None)

    daily = DailyMenus()
    daily.business_id = business.bussiness_id
    daily.fecha_vigencia = today
    daily.status = DailyMenuStatus.ACTIVO.value
    daily.schema_version = 3
    daily.extracted_menu = make_menu().model_dump()
    fake_store.seed(daily)

    result = get_today_menu(FakeToolContext(business.bussiness_id))

    assert result["menu_available"] is True
    assert "Pollo al curry con champiñones" in result["text"]


def test_active_menu_reads_legacy_plato_del_dia_key(fake_store):
    """Documentos guardados antes del rename de 2026-09-23 (schema_version
    2, clave "plato_del_dia") se siguen leyendo -- ver validation_alias en
    ExtractedMenu (vision_extractor.py)."""
    business = seed_business(fake_store)
    today = resolve_fecha_vigencia(None)

    daily = DailyMenus()
    daily.business_id = business.bussiness_id
    daily.fecha_vigencia = today
    daily.status = DailyMenuStatus.ACTIVO.value
    daily.schema_version = 2
    legacy = make_menu().model_dump()
    legacy["plato_del_dia"] = legacy.pop("menu_del_dia")
    daily.extracted_menu = legacy
    fake_store.seed(daily)

    result = get_today_menu(FakeToolContext(business.bussiness_id))

    assert result["menu_available"] is True
    assert "Pollo al curry con champiñones" in result["text"]
    assert "$18.500" in result["text"]
    assert "Cambios respecto al anterior" not in result["text"]


def _seed_active(fake_store, business, fecha):
    daily = DailyMenus()
    daily.business_id = business.bussiness_id
    daily.fecha_vigencia = fecha
    daily.status = DailyMenuStatus.ACTIVO.value
    daily.schema_version = 3
    daily.menu_media_id = "media-ayer"
    daily.extracted_menu = make_menu().model_dump()
    fake_store.seed(daily)
    return daily


def test_reuse_last_menu_copies_last_active_to_today_and_reopens(fake_store):
    business = seed_business(fake_store)
    today = resolve_fecha_vigencia(None)
    _seed_active(fake_store, business, "2000-01-01")
    DailyMenuService().mark_closed_today(business.bussiness_id, today, None, "vacaciones")

    result = reuse_last_menu(FakeToolContext(business.bussiness_id))

    assert result["ok"] is True and result["text"]
    service = DailyMenuService()
    active = service.get_active_for_date(business.bussiness_id, today)
    assert active.menu_media_id == "media-ayer"
    assert service.get_closed_for_date(business.bussiness_id, today) is None
    assert get_today_menu(FakeToolContext(business.bussiness_id))["menu_available"] is True


def test_reuse_last_menu_reasons(fake_store):
    business = seed_business(fake_store)
    ctx = FakeToolContext(business.bussiness_id)
    assert reuse_last_menu(ctx) == {"ok": False, "reason": "sin_menu_anterior"}

    _seed_active(fake_store, business, resolve_fecha_vigencia(None))
    assert reuse_last_menu(ctx) == {"ok": False, "reason": "ya_hay_menu_hoy"}


def test_confirm_after_closed_today_reopens_the_day(fake_store):
    business = seed_business(fake_store)
    today = resolve_fecha_vigencia(None)
    service = DailyMenuService()
    service.mark_closed_today(business.bussiness_id, today, None, None)
    draft = service.create_draft(business.bussiness_id, "573001111111", today, make_menu(), "m", "/tmp/m.jpg", "x", None)

    service.confirm(draft)

    assert service.get_closed_for_date(business.bussiness_id, today) is None
    assert get_today_menu(FakeToolContext(business.bussiness_id))["menu_available"] is True


def test_activating_carta_after_closed_today_reopens_the_day(fake_store):
    # Real 2026-10-21: "hoy no abro" y luego sube la carta -> seguía cerrado.
    business = seed_business(fake_store)
    today = resolve_fecha_vigencia(None)
    service = DailyMenuService()
    service.mark_closed_today(business.bussiness_id, today, None, None)
    draft = service.add_carta_page(business.bussiness_id, "573001111111", today, [], "m1")

    service.activate_carta(draft, merge=False)

    assert service.get_closed_for_date(business.bussiness_id, today) is None
