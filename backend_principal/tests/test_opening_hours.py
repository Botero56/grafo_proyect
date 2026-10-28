"""Horario de atención por negocio: rangos, medianoche, excepciones, la zona
horaria del negocio (el servidor corre en UTC) y el "hoy no abrimos" del chat."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.ai.ai_restaurant.user.tools.order_tools import confirm_order
from app.config import admin_api_settings
from app.firestore_db.firebase_schemas import Configurations, DailyMenus
from app.schemas.enum_schemas import DailyMenuStatus
from app.services import opening_hours, tenant_profile, worker_entry
from app.services.opening_hours import evaluate
from tests.conftest import seed_business, seed_configuration
from tests.test_order_state import _ready_order

HOURS = {
    "timezone": "America/Bogota",
    "weekly": {
        # lunes: almuerzo y noche que cruza la medianoche
        "mon": [{"open": "11:00", "close": "15:00"}, {"open": "18:00", "close": "02:00"}],
        "tue": [{"open": "11:00", "close": "15:00"}],
    },
}


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# 2026-10-12 es lunes. Bogotá = UTC-5.


def test_inside_and_outside_a_range():
    assert evaluate(HOURS, _utc(2026, 10, 12, 17, 0))["open"] is True  # lun 12:00 local
    closed = evaluate(HOURS, _utc(2026, 10, 12, 21, 0))  # lun 16:00 local
    assert closed["open"] is False
    assert closed["next_opening"].strftime("%Y-%m-%d %H:%M") == "2026-10-12 18:00"


def test_range_crossing_midnight_stays_open_the_next_day():
    assert evaluate(HOURS, _utc(2026, 10, 13, 6, 30))["open"] is True  # mar 01:30 local
    after = evaluate(HOURS, _utc(2026, 10, 13, 7, 30))  # mar 02:30 local
    assert after["open"] is False
    assert after["next_opening"].strftime("%a %H:%M") == "Tue 11:00"


def test_the_business_time_zone_wins_over_the_server_utc_date():
    # 2026-10-13 03:00 UTC ya es martes en UTC, pero en Bogotá es lunes 22:00.
    assert evaluate(HOURS, _utc(2026, 10, 13, 3, 0))["open"] is True
    madrid = {**HOURS, "timezone": "Europe/Madrid"}  # UTC+2 en octubre
    assert evaluate(madrid, _utc(2026, 10, 12, 10, 0))["open"] is True  # 12:00 en Madrid


def test_exceptions_replace_the_weekly_hours_of_that_day():
    closed_day = {**HOURS, "exceptions": [{"date": "2026-10-12", "closed": True}]}
    at_noon = evaluate(closed_day, _utc(2026, 10, 12, 17, 0))
    assert at_noon["open"] is False
    assert at_noon["next_opening"].strftime("%Y-%m-%d %H:%M") == "2026-10-13 11:00"

    short_day = {**HOURS, "exceptions": [{"date": "2026-10-13", "ranges": [{"open": "09:00", "close": "10:00"}]}]}
    assert evaluate(short_day, _utc(2026, 10, 13, 14, 30))["open"] is True  # mar 09:30
    assert evaluate(short_day, _utc(2026, 10, 13, 17, 0))["open"] is False  # mar 12:00


def test_closed_by_the_admin_chat_counts_as_a_closed_exception():
    assert evaluate(HOURS, _utc(2026, 10, 12, 17, 0), frozenset({"2026-10-12"}))["open"] is False


def test_next_opening_is_described_relative_to_today():
    now = _utc(2026, 10, 12, 21, 0)  # lun 16:00
    assert opening_hours.describe_next_opening(evaluate(HOURS, now)["next_opening"], now) == "hoy a las 18:00"
    late = _utc(2026, 10, 13, 20, 30)  # mar 15:30: el próximo es el lunes
    assert opening_hours.describe_next_opening(evaluate(HOURS, late)["next_opening"], late) == "el lunes a las 11:00"


def test_without_hours_there_is_no_restriction(fake_store):
    business = seed_business(fake_store)
    state = opening_hours.status(business.bussiness_id, {})
    assert state == {"configured": False, "open": True, "enforce": False, "next_opening": None}


def test_status_reads_the_closed_day_written_by_mark_closed_today(fake_store):
    business = seed_business(fake_store)
    record = DailyMenus()
    record.business_id = business.bussiness_id
    record.fecha_vigencia = "2026-10-12"
    record.status = DailyMenuStatus.CERRADO.value
    fake_store.seed(record)
    opening_hours.invalidate(business.bussiness_id)

    assert opening_hours.status(business.bussiness_id, HOURS, _utc(2026, 10, 12, 17, 0))["open"] is False


async def test_confirm_order_is_rejected_outside_hours(fake_store):
    ctx = await _ready_order(fake_store)
    configuration = next(iter(fake_store.stores[Configurations].values()))
    configuration.opening_hours = {"timezone": "America/Bogota", "exceptions": [{"date": "2000-01-01", "closed": True}]}
    tenant_profile.invalidate(ctx.state["business_id"])

    result = await confirm_order(ctx)

    assert result["ok"] is False
    assert "cerrado" in result["errores"][0]
    assert "cerrado" in tenant_profile.render_profile(ctx.state["business_id"]).lower()


async def test_confirm_order_only_informs_when_enforce_is_off(fake_store):
    from unittest.mock import AsyncMock, patch

    ctx = await _ready_order(fake_store)
    configuration = next(iter(fake_store.stores[Configurations].values()))
    configuration.opening_hours = {"exceptions": [{"date": "2000-01-01", "closed": True}], "enforce": False}
    tenant_profile.invalidate(ctx.state["business_id"])

    with patch("app.services.printer_client.print_jobs.PrinterClient") as printer_cls:
        printer_cls.return_value.print_text = AsyncMock(return_value=True)
        result = await confirm_order(ctx)

    assert result["ok"] is True


def test_patch_validates_and_stores_opening_hours(fake_store):
    business = seed_business(fake_store)
    configuration = seed_configuration(fake_store, business)
    client = TestClient(worker_entry.app)
    headers = {"X-API-Key": admin_api_settings.API_KEY}
    url = f"/api/v1/admin/configurations/{business.bussiness_id}"

    ok = client.patch(url, json={"opening_hours": {**HOURS, "exceptions": [{"date": "2026-12-25", "closed": True}]}}, headers=headers)
    bad_time = client.patch(url, json={"opening_hours": {"weekly": {"mon": [{"open": "25:00", "close": "26:00"}]}}}, headers=headers)
    bad_tz = client.patch(url, json={"opening_hours": {"timezone": "Marte/Olympus"}}, headers=headers)
    bad_exception = client.patch(url, json={"opening_hours": {"exceptions": [{"date": "2026-12-25"}]}}, headers=headers)

    assert ok.status_code == 200, ok.text
    stored = fake_store.stores[Configurations][configuration.configuration_id].opening_hours
    assert stored["exceptions"] == [{"date": "2026-12-25", "closed": True}]
    assert stored["weekly"]["mon"][1] == {"open": "18:00", "close": "02:00"}
    assert bad_time.status_code == bad_tz.status_code == bad_exception.status_code == 422
