from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.config import daily_menu_settings
from app.firestore_db.firebase_schemas import DailyMenus
from app.schemas.enum_schemas import DailyMenuStatus
from app.services.daily_menu.reminders import expire_pending_daily_menus, remind_missing_daily_menus, reminder_due
from app.services.excel_report_service.constants import DEFAULT_TZ
from tests.conftest import seed_business, seed_configuration, seed_conversation, seed_customer


def _seed_pending_draft(fake_store, business, *, created_at=None, reminder_sent_at=None) -> DailyMenus:
    draft = DailyMenus()
    draft.business_id = business.bussiness_id
    draft.admin_phone = "573001111111"
    draft.fecha_vigencia = "2026-09-21"
    draft.status = DailyMenuStatus.PENDIENTE_CONFIRMACION.value
    draft.created_at = created_at or datetime.now(timezone.utc)
    draft.reminder_sent_at = reminder_sent_at
    return fake_store.seed(draft)


@pytest.mark.asyncio
async def test_reminds_once_then_discards_after_second_cutoff(fake_store):
    business = seed_business(fake_store)
    now = datetime.now(timezone.utc)

    stale = _seed_pending_draft(
        fake_store,
        business,
        created_at=now - timedelta(minutes=daily_menu_settings.DAILY_MENU_REMINDER_MINUTES + 5),
    )

    with patch("app.services.daily_menu.reminders.WhatsAppClient") as ClientCls:
        ClientCls.for_business.return_value.send_message = AsyncMock(return_value=True)
        result = await expire_pending_daily_menus(now=now)

    assert stale.daily_menu_id in result["reminded"]
    assert stale.reminder_sent_at == now
    assert stale.status == DailyMenuStatus.PENDIENTE_CONFIRMACION.value

    later = now + timedelta(minutes=daily_menu_settings.DAILY_MENU_DISCARD_MINUTES + 5)
    with patch("app.services.daily_menu.reminders.WhatsAppClient") as ClientCls2:
        ClientCls2.for_business.return_value.send_message = AsyncMock(return_value=True)
        result2 = await expire_pending_daily_menus(now=later)

    assert stale.daily_menu_id in result2["discarded"]
    assert stale.status == DailyMenuStatus.DESCARTADO.value


@pytest.mark.asyncio
async def test_fresh_pending_draft_is_not_reminded_yet(fake_store):
    business = seed_business(fake_store)
    now = datetime.now(timezone.utc)
    fresh = _seed_pending_draft(fake_store, business, created_at=now)

    with patch("app.services.daily_menu.reminders.WhatsAppClient") as ClientCls:
        ClientCls.for_business.return_value.send_message = AsyncMock(return_value=True)
        result = await expire_pending_daily_menus(now=now)

    assert fresh.daily_menu_id not in result["reminded"]
    assert fresh.status == DailyMenuStatus.PENDIENTE_CONFIRMACION.value


@pytest.mark.parametrize(
    "hora, now, due",
    [
        ("10:00", datetime(2026, 9, 21, 10, 0, tzinfo=DEFAULT_TZ), True),
        ("10:00", datetime(2026, 9, 21, 10, 9, 59, tzinfo=DEFAULT_TZ), True),
        ("10:00", datetime(2026, 9, 21, 10, 10, tzinfo=DEFAULT_TZ), False),  # ya lo recordó la corrida anterior
        ("10:00", datetime(2026, 9, 21, 9, 59, tzinfo=DEFAULT_TZ), False),
        (None, datetime(2026, 9, 21, 10, 0, tzinfo=DEFAULT_TZ), False),  # negocio que no usa menú del día
    ],
)
def test_each_business_is_reminded_once_at_its_own_hour(hora, now, due):
    assert reminder_due(hora, now) is due


@pytest.mark.asyncio
async def test_remind_missing_skips_businesses_without_or_before_their_hour(fake_store):
    for hora in (None, "11:00"):
        business = seed_business(fake_store)
        seed_configuration(fake_store, business, admin_phone_numbers=["573001111111"], recordatorio_menu_hora=hora)

    with patch("app.services.daily_menu.reminders.WhatsAppClient") as ClientCls:
        ClientCls.for_business.return_value.send_message = AsyncMock(return_value=True)
        result = await remind_missing_daily_menus(now=datetime(2026, 9, 21, 10, 0, tzinfo=DEFAULT_TZ))

    assert result["reminded"] == []


@pytest.mark.asyncio
async def test_remind_missing_skips_business_with_active_menu_today(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, admin_phone_numbers=["573001111111"], recordatorio_menu_hora="10:00")

    from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia

    today = resolve_fecha_vigencia(None)
    active = DailyMenus()
    active.business_id = business.bussiness_id
    active.fecha_vigencia = today
    active.status = DailyMenuStatus.ACTIVO.value
    fake_store.seed(active)

    late = datetime(2026, 9, 21, 10, 0, tzinfo=DEFAULT_TZ)
    with patch("app.services.daily_menu.reminders.WhatsAppClient") as ClientCls:
        ClientCls.for_business.return_value.send_message = AsyncMock(return_value=True)
        result = await remind_missing_daily_menus(now=late)

    assert result["reminded"] == []


@pytest.mark.asyncio
async def test_remind_missing_skips_business_closed_today(fake_store):
    from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia
    from app.services.daily_menu.daily_menu_service import DailyMenuService

    business = seed_business(fake_store)
    seed_configuration(fake_store, business, admin_phone_numbers=["573001111111"], recordatorio_menu_hora="10:00")

    late = datetime(2026, 9, 21, 10, 0, tzinfo=DEFAULT_TZ)
    today = resolve_fecha_vigencia(None, late)
    DailyMenuService().mark_closed_today(business.bussiness_id, today, "573001111111", "vacaciones")

    with patch("app.services.daily_menu.reminders.WhatsAppClient") as ClientCls:
        ClientCls.for_business.return_value.send_message = AsyncMock(return_value=True)
        result = await remind_missing_daily_menus(now=late)

    assert result["reminded"] == []


@pytest.mark.asyncio
async def test_remind_missing_sends_freeform_within_24h_window(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, admin_phone_numbers=["573001111111"], recordatorio_menu_hora="10:00")

    customer = seed_customer(fake_store, business, "573001111111")
    seed_conversation(fake_store, business, customer.customer_id, ultimo_mensaje_cliente_at=datetime.now(timezone.utc))

    late = datetime(2026, 9, 21, 10, 0, tzinfo=DEFAULT_TZ)
    with patch("app.services.daily_menu.reminders.WhatsAppClient") as ClientCls:
        instance = ClientCls.for_business.return_value
        instance.send_message = AsyncMock(return_value=True)
        instance.send_template = AsyncMock(return_value=True)
        result = await remind_missing_daily_menus(now=late)

    instance.send_message.assert_awaited_once()
    instance.send_template.assert_not_awaited()
    assert (business.bussiness_id, "573001111111") in result["reminded"]


@pytest.mark.asyncio
async def test_remind_missing_uses_template_outside_24h_window(fake_store):
    business = seed_business(fake_store)
    seed_configuration(
        fake_store,
        business,
        admin_phone_numbers=["573001111111"],
        recordatorio_menu_hora="10:00",
        admin_templates={"recordatorio_menu": {"name": "menu_reminder", "language": "es_CO"}},
    )
    # Sin Customer/Conversation seedeado => nunca escribió => fuera de 24h.

    late = datetime(2026, 9, 21, 10, 0, tzinfo=DEFAULT_TZ)
    with patch("app.services.daily_menu.reminders.WhatsAppClient") as ClientCls:
        instance = ClientCls.for_business.return_value
        instance.send_message = AsyncMock(return_value=True)
        instance.send_template = AsyncMock(return_value=True)
        result = await remind_missing_daily_menus(now=late)

    instance.send_template.assert_awaited_once_with(
        "573001111111", "menu_reminder", "es_CO", ["aún no tengo el menú de hoy, envíame la foto por este chat"]
    )
    instance.send_message.assert_not_awaited()
    assert (business.bussiness_id, "573001111111") in result["reminded"]


@pytest.mark.asyncio
async def test_remind_missing_uses_the_business_text_in_template_and_freeform(fake_store):
    from app.schemas.api.configuration_schemas import ConfigurationUpdateRequest

    # Lo que manda el panel: una sola línea y sin punto final.
    texto = ConfigurationUpdateRequest(recordatorio_menu_texto="  recuerda revisar\nla carta.  ").recordatorio_menu_texto
    assert texto == "recuerda revisar la carta"

    business = seed_business(fake_store)
    seed_configuration(
        fake_store,
        business,
        admin_phone_numbers=["573001111111", "573002222222"],
        recordatorio_menu_hora="10:00",
        recordatorio_menu_texto=texto,
        admin_templates={"recordatorio_menu": {"name": "j2ys_recordatorio_admin", "language": "es"}},
    )
    customer = seed_customer(fake_store, business, "573002222222")
    seed_conversation(fake_store, business, customer.customer_id, ultimo_mensaje_cliente_at=datetime.now(timezone.utc))

    late = datetime(2026, 9, 21, 10, 0, tzinfo=DEFAULT_TZ)
    with patch("app.services.daily_menu.reminders.WhatsAppClient") as ClientCls:
        instance = ClientCls.for_business.return_value
        instance.send_message = AsyncMock(return_value=True)
        instance.send_template = AsyncMock(return_value=True)
        await remind_missing_daily_menus(now=late)

    instance.send_template.assert_awaited_once_with("573001111111", "j2ys_recordatorio_admin", "es", [texto])
    instance.send_message.assert_awaited_once_with(
        "573002222222",
        "Hola, te escribe tu asistente de pedidos: recuerda revisar la carta. Responde a este mensaje cuando lo tengas listo.",
    )


@pytest.mark.asyncio
async def test_remind_missing_logs_an_error_without_template_outside_24h(fake_store, caplog):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, admin_phone_numbers=["573001111111"], recordatorio_menu_hora="10:00")

    late = datetime(2026, 9, 21, 10, 0, tzinfo=DEFAULT_TZ)
    with patch("app.services.daily_menu.reminders.WhatsAppClient") as ClientCls:
        instance = ClientCls.for_business.return_value
        instance.send_message = AsyncMock(return_value=True)
        instance.send_template = AsyncMock(return_value=True)
        result = await remind_missing_daily_menus(now=late)

    instance.send_template.assert_not_awaited()
    instance.send_message.assert_not_awaited()
    assert result["reminded"] == []
    assert any(r.levelname == "ERROR" and "NO entregado" in r.message for r in caplog.records)


def test_the_migration_keeps_today_s_reminder_only_where_it_was_sent():
    from ops.set_reminder_hour import planned_changes

    docs = [
        ("a", {"business_id": "A", "admin_phone_numbers": ["573001"]}),  # hoy le llega -> 09:30
        ("b", {"business_id": "B", "admin_phone_numbers": ["573001"]}),  # negocio inactivo
        ("c", {"business_id": "A2", "admin_phone_numbers": []}),  # sin admins: hoy no le llega
        ("d", {"business_id": "A3", "admin_phone_numbers": ["573001"], "recordatorio_menu_hora": None}),  # ya decidido
    ]
    assert planned_changes(docs, {"A", "A2", "A3"}) == [("a", {"recordatorio_menu_hora": "09:30"})]
