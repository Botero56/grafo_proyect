from app.firestore_db.firebase_schemas import DailyMenus
from app.schemas.enum_schemas import DailyMenuStatus
from app.services.daily_menu.daily_menu_service import SCHEMA_VERSION, DailyMenuService
from tests.conftest import make_menu, seed_business


def _create_draft(business_id: str, fecha_vigencia: str = "2026-09-21", precio=18500) -> DailyMenus:
    return DailyMenuService().create_draft(
        business_id=business_id,
        admin_phone="573001111111",
        fecha_vigencia=fecha_vigencia,
        extracted_menu=make_menu(precio=precio),
        source_media_id="m1",
        source_image_path="/tmp/m1.jpg",
        source_sha256="abc",
        caption=None,
    )


def test_create_draft_stamps_schema_version(fake_store):
    business = seed_business(fake_store)
    draft = _create_draft(business.bussiness_id)
    assert draft.schema_version == SCHEMA_VERSION == 3


def test_create_draft_discards_previous_pending(fake_store):
    business = seed_business(fake_store)
    first = _create_draft(business.bussiness_id)
    second = _create_draft(business.bussiness_id)

    assert first.status == DailyMenuStatus.DESCARTADO.value
    assert second.status == DailyMenuStatus.PENDIENTE_CONFIRMACION.value
    assert DailyMenuService().get_pending(business.bussiness_id).daily_menu_id == second.daily_menu_id


def test_confirm_marks_previous_active_same_date_as_reemplazado(fake_store):
    business = seed_business(fake_store)
    draft1 = _create_draft(business.bussiness_id, fecha_vigencia="2026-09-21")
    DailyMenuService().confirm(draft1)
    assert draft1.status == DailyMenuStatus.ACTIVO.value

    draft2 = _create_draft(business.bussiness_id, fecha_vigencia="2026-09-21", precio=25000)
    DailyMenuService().confirm(draft2)

    assert draft1.status == DailyMenuStatus.REEMPLAZADO.value
    assert draft2.status == DailyMenuStatus.ACTIVO.value
    assert (
        DailyMenuService().get_active_for_date(business.bussiness_id, "2026-09-21").daily_menu_id
        == draft2.daily_menu_id
    )


def test_confirm_does_not_touch_active_menu_of_a_different_date(fake_store):
    business = seed_business(fake_store)
    yesterday = _create_draft(business.bussiness_id, fecha_vigencia="2026-09-20")
    DailyMenuService().confirm(yesterday)

    today = _create_draft(business.bussiness_id, fecha_vigencia="2026-09-21")
    DailyMenuService().confirm(today)

    assert yesterday.status == DailyMenuStatus.ACTIVO.value
    assert today.status == DailyMenuStatus.ACTIVO.value


def test_cancel_marks_descartado(fake_store):
    business = seed_business(fake_store)
    draft = _create_draft(business.bussiness_id)
    DailyMenuService().cancel(draft)
    assert draft.status == DailyMenuStatus.DESCARTADO.value
    assert draft.discarded_at is not None


def test_apply_correction_updates_menu_and_history(fake_store):
    business = seed_business(fake_store)
    draft = _create_draft(business.bussiness_id, precio=18500)
    updated = make_menu(precio=19000)

    DailyMenuService().apply_correction(draft, "el plato del día vale 19.000", updated)

    assert draft.correction_history == ["el plato del día vale 19.000"]
    assert draft.extracted_menu["menu_del_dia"]["precio"] == 19000
    assert draft.status == DailyMenuStatus.PENDIENTE_CONFIRMACION.value


def test_get_active_for_date_ignores_other_dates(fake_store):
    business = seed_business(fake_store)
    draft = _create_draft(business.bussiness_id, fecha_vigencia="2026-09-20")
    DailyMenuService().confirm(draft)

    assert DailyMenuService().get_active_for_date(business.bussiness_id, "2026-09-21") is None
    assert (
        DailyMenuService().get_active_for_date(business.bussiness_id, "2026-09-20").daily_menu_id
        == draft.daily_menu_id
    )


def test_get_last_active_returns_most_recent_by_date(fake_store):
    business = seed_business(fake_store)
    older = _create_draft(business.bussiness_id, fecha_vigencia="2026-09-19")
    DailyMenuService().confirm(older)
    newer = _create_draft(business.bussiness_id, fecha_vigencia="2026-09-20")
    DailyMenuService().confirm(newer)

    last_active = DailyMenuService().get_last_active(business.bussiness_id)

    assert last_active.daily_menu_id == newer.daily_menu_id


def test_get_last_active_returns_none_when_never_confirmed(fake_store):
    business = seed_business(fake_store)
    _create_draft(business.bussiness_id)  # queda pendiente, nunca confirmado

    assert DailyMenuService().get_last_active(business.bussiness_id) is None


def test_mark_closed_today_creates_closed_record(fake_store):
    business = seed_business(fake_store)

    record = DailyMenuService().mark_closed_today(
        business.bussiness_id, "2026-09-22", "573001111111", "cerrado por remodelación"
    )

    assert record.status == DailyMenuStatus.CERRADO.value
    assert record.caption == "cerrado por remodelación"
    assert record.admin_phone == "573001111111"
    closed = DailyMenuService().get_closed_for_date(business.bussiness_id, "2026-09-22")
    assert closed.daily_menu_id == record.daily_menu_id


def test_mark_closed_today_discards_pending_draft(fake_store):
    business = seed_business(fake_store)
    draft = _create_draft(business.bussiness_id, fecha_vigencia="2026-09-22")

    DailyMenuService().mark_closed_today(business.bussiness_id, "2026-09-22", None, None)

    assert draft.status == DailyMenuStatus.DESCARTADO.value


def test_mark_closed_today_replaces_existing_active_menu(fake_store):
    business = seed_business(fake_store)
    draft = _create_draft(business.bussiness_id, fecha_vigencia="2026-09-22")
    DailyMenuService().confirm(draft)

    DailyMenuService().mark_closed_today(business.bussiness_id, "2026-09-22", None, "vacaciones")

    assert draft.status == DailyMenuStatus.REEMPLAZADO.value
    assert DailyMenuService().get_active_for_date(business.bussiness_id, "2026-09-22") is None


def test_mark_closed_today_called_twice_updates_reason_instead_of_duplicating(fake_store):
    business = seed_business(fake_store)

    first = DailyMenuService().mark_closed_today(business.bussiness_id, "2026-09-22", None, "motivo 1")
    second = DailyMenuService().mark_closed_today(business.bussiness_id, "2026-09-22", None, "motivo 2")

    assert first.daily_menu_id == second.daily_menu_id
    assert second.caption == "motivo 2"
