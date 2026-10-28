from datetime import datetime, timedelta, timezone

from unittest.mock import AsyncMock, patch

import pytest

from app.config import handoff_settings
from app.firestore_db.firebase_schemas import Conversations
from app.services.conversation.handoff_service import HandoffService
from tests.conftest import seed_business, seed_configuration, seed_conversation, seed_customer


def _seed_conversation(fake_store, business) -> Conversations:
    return seed_conversation(fake_store, business, "cust_1", channel="whatsapp", bot_activo=True)


def test_pause_bot_sets_expected_fields(fake_store):
    business = seed_business(fake_store)
    conversation = _seed_conversation(fake_store, business)

    HandoffService().pause_bot(conversation, motivo="solicitud_cliente", agente_asignado="ana")

    assert conversation.bot_activo is False
    assert conversation.motivo_handoff == "solicitud_cliente"
    assert conversation.agente_asignado == "ana"
    assert conversation.pausado_desde is not None


def test_reactivate_bot_clears_handoff_fields(fake_store):
    business = seed_business(fake_store)
    conversation = _seed_conversation(fake_store, business)
    HandoffService().pause_bot(conversation, motivo="baja_confianza")

    HandoffService().reactivate_bot(conversation)

    assert conversation.bot_activo is True
    assert conversation.motivo_handoff is None
    assert conversation.pausado_desde is None
    assert conversation.pausado_hasta is None


def test_transfer_message_falls_back_to_default_when_not_configured(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)  # sin handoff_transfer_message

    message = HandoffService().get_transfer_message(business.bussiness_id)

    assert message == handoff_settings.HANDOFF_DEFAULT_TRANSFER_MESSAGE


def test_transfer_message_uses_business_override(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, handoff_transfer_message="Un momento, ya te atendemos")

    message = HandoffService().get_transfer_message(business.bussiness_id)

    assert message == "Un momento, ya te atendemos"


def test_should_not_auto_reactivate_within_quick_window(fake_store):
    business = seed_business(fake_store)
    conversation = _seed_conversation(fake_store, business)
    HandoffService().pause_bot(conversation, motivo="respuesta_manual")
    conversation.ultimo_mensaje_humano_at = datetime.now(timezone.utc)

    assert HandoffService().should_auto_reactivate(conversation) is False


def test_should_auto_reactivate_after_quick_window_elapsed(fake_store):
    business = seed_business(fake_store)
    conversation = _seed_conversation(fake_store, business)
    HandoffService().pause_bot(conversation, motivo="respuesta_manual")
    conversation.ultimo_mensaje_humano_at = datetime.now(timezone.utc) - timedelta(
        seconds=handoff_settings.HANDOFF_QUICK_REACTIVATION_SECONDS + 1
    )

    assert HandoffService().should_auto_reactivate(conversation) is True


def test_should_auto_reactivate_falls_back_to_pausado_desde_without_echo(fake_store):
    business = seed_business(fake_store)
    conversation = _seed_conversation(fake_store, business)
    HandoffService().pause_bot(conversation, motivo="baja_confianza")
    conversation.pausado_desde = datetime.now(timezone.utc) - timedelta(
        seconds=handoff_settings.HANDOFF_QUICK_REACTIVATION_SECONDS + 1
    )
    # nunca hubo un echo (ultimo_mensaje_humano_at sigue None)

    assert HandoffService().should_auto_reactivate(conversation) is True


def test_should_auto_reactivate_true_when_no_reference_at_all(fake_store):
    business = seed_business(fake_store)
    conversation = _seed_conversation(fake_store, business)
    conversation.bot_activo = False
    # Ni ultimo_mensaje_humano_at ni pausado_desde -- fail-safe: no se
    # queda pausada para siempre sin ninguna referencia de tiempo.

    assert HandoffService().should_auto_reactivate(conversation) is True


@pytest.mark.asyncio
async def test_notify_human_returns_false_without_manager_phone(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, manager_phone=None)

    class DummyCustomer:
        name = "Cliente"
        phone_number = "573000000000"

    notified = await HandoffService().notify_human(business, DummyCustomer(), motivo="solicitud_cliente")

    assert notified is False


class _Customer:
    name = "Cliente"
    phone_number = "573000000000"


def _admin_wrote_recently(fake_store, business, phone):
    """El admin le escribió al negocio hace poco: está dentro de la ventana
    de 24h de WhatsApp."""
    admin = seed_customer(fake_store, business, phone)
    seed_conversation(
        fake_store, business, admin.customer_id, ultimo_mensaje_cliente_at=datetime.now(timezone.utc)
    )


@pytest.mark.asyncio
async def test_notify_human_falls_back_to_the_admins_without_manager_phone(fake_store):
    """Regresión (prueba real 2026-09-30): sin manager_phone no se avisaba a
    nadie, aunque el negocio tuviera admins configurados."""
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, manager_phone=None, admin_phone_numbers=["573133084564"])
    _admin_wrote_recently(fake_store, business, "573133084564")

    with patch("app.services.conversation.handoff_service.WhatsAppClient") as client_cls:
        client = client_cls.for_business.return_value
        client.send_message = AsyncMock(return_value=True)
        notified = await HandoffService().notify_human(business, _Customer(), motivo="solicitud_cliente")

    assert notified is True
    client.send_message.assert_awaited_once()
    assert client.send_message.await_args.args[0] == "573133084564"


@pytest.mark.asyncio
async def test_notify_human_uses_the_template_outside_the_24h_window(fake_store):
    business = seed_business(fake_store)
    seed_configuration(
        fake_store,
        business,
        manager_phone="573009999999",  # nunca escribió
        admin_templates={"atencion_cliente": {"name": "aviso_handoff", "language": "es"}},
    )

    with patch("app.services.conversation.handoff_service.WhatsAppClient") as client_cls:
        client = client_cls.for_business.return_value
        client.send_message = AsyncMock(return_value=True)
        client.send_template = AsyncMock(return_value=True)
        notified = await HandoffService().notify_human(business, _Customer(), motivo="solicitud_cliente")

    assert notified is True
    client.send_template.assert_awaited_once_with(
        "573009999999", "aviso_handoff", "es", ["🔔 Cliente necesita atención humana (solicitud_cliente)."]
    )
    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_human_logs_an_error_when_it_cannot_reach_anyone(fake_store, caplog):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, manager_phone="573009999999")  # nunca escribió

    with patch("app.services.conversation.handoff_service.WhatsAppClient") as client_cls:
        client = client_cls.for_business.return_value
        client.send_message = AsyncMock(return_value=True)
        notified = await HandoffService().notify_human(business, _Customer(), motivo="solicitud_cliente")

    assert notified is False
    client.send_message.assert_not_awaited()
    undelivered = [r for r in caplog.records if getattr(r, "fields", {}).get("ops_event") == "admin_notice_undelivered"]
    assert undelivered and undelivered[0].levelname == "ERROR"
    assert "9999" in undelivered[0].getMessage() and "573009999999" not in undelivered[0].getMessage()


async def test_the_handoff_note_is_short_with_the_plain_phone(fake_store):
    """La encargada recibe quién es, qué pasó y para qué la buscan; el
    teléfono como número (no enlace) para buscarlo en WhatsApp."""
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, manager_name="Doña Diana")
    customer = seed_customer(fake_store, business, phone="573001234567", name="Ana")

    with patch.object(HandoffService, "notify_admins", new=AsyncMock(return_value=True)) as notify:
        await HandoffService().notify_handoff(business, customer, "Reclamo por un pedido incompleto")

    assert notify.await_args.args[1] == (
        "🚨 Cliente solicita atención\n"
        "👤 Ana\n"
        "📱 3001234567\n"
        "📝 Reclamo por un pedido incompleto.\n"
        "➡️ Necesita hablar con Doña Diana."
    )


async def test_the_handoff_note_without_name_or_configured_manager(fake_store):
    business = seed_business(fake_store)
    customer = seed_customer(fake_store, business, phone="573001234567", name="Cliente Nuevo")

    with patch.object(HandoffService, "notify_admins", new=AsyncMock(return_value=True)) as notify:
        await HandoffService().notify_handoff(business, customer, "Inconformidad con el cobro", tema="el domicilio de hoy")

    assert notify.await_args.args[1] == (
        "🚨 Cliente solicita atención\n"
        "📱 3001234567\n"
        "📝 Inconformidad con el cobro.\n"
        "➡️ Necesita hablar con la administradora sobre el domicilio de hoy."
    )
