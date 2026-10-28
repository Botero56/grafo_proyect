"""Regla de la imagen del menú: sale sola la primera vez del día (o si el
admin publicó otra), antes de la respuesta, sin depender del modelo, y
nunca es la foto de otro día."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.ai.core.agent_runner import AgentReply
from app.firestore_db.firebase_schemas import DailyMenus, Messages
from app.schemas.enum_schemas import DailyMenuStatus
from app.services.daily_menu import menu_image
from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia
from app.services.daily_menu.daily_menu_service import DailyMenuService
from app.services.messaging import inbound_pipeline
from tests.conftest import (
    DummyHandlers,
    seed_active_menu,
    seed_business,
    seed_configuration,
    seed_conversation,
    seed_customer,
)

def _today() -> str:
    """"Hoy" en la zona del negocio, calculado en cada test: una constante
    del módulo quedaba fija al importar y fallaba si la suite cruzaba la
    medianoche de Bogotá."""
    return resolve_fecha_vigencia(None)


def _whatsapp():
    client = AsyncMock()
    client.send_image = AsyncMock(return_value=True)
    return client


def _menu(fake_store, business, media_id, fecha=None):
    # seed_active_menu manda sus overrides al extracted_menu, no al documento.
    menu = seed_active_menu(fake_store, business, fecha_vigencia=fecha)
    menu.menu_media_id = media_id
    return menu


def _context(fake_store, **config):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, **config)
    customer = seed_customer(fake_store, business)
    conversation = seed_conversation(fake_store, business, customer.customer_id, bot_activo=True)
    return business, customer, conversation


def test_today_active_menu_wins(fake_store):
    business, _, _ = _context(fake_store, carta_media_id="CARTA")
    menu = _menu(fake_store, business, "HOY")

    assert DailyMenuService().current_menu_image(business.bussiness_id, _today()) == (menu.daily_menu_id, ["HOY"], None)


def test_without_menu_today_the_carta_is_used(fake_store):
    business, _, _ = _context(fake_store, carta_media_id="CARTA")

    assert DailyMenuService().current_menu_image(business.bussiness_id, _today()) == ("CARTA", ["CARTA"], None)


def test_yesterdays_menu_photo_left_in_carta_is_never_sent(fake_store):
    """Antes se guardaba la foto del menú del día también en carta_media_id:
    al día siguiente el bot habría mandado el menú de ayer."""
    business, _, _ = _context(fake_store, carta_media_id="AYER")
    yesterday = (date.fromisoformat(_today()) - timedelta(days=1)).isoformat()
    _menu(fake_store, business, "AYER", yesterday)

    assert DailyMenuService().current_menu_image(business.bussiness_id, _today()) is None


def test_closed_today_has_no_image(fake_store):
    business, _, _ = _context(fake_store, carta_media_id="CARTA")
    DailyMenuService().mark_closed_today(business.bussiness_id, _today(), None, "remodelación")

    assert DailyMenuService().current_menu_image(business.bussiness_id, _today()) is None


async def test_auto_sends_once_per_version(fake_store):
    business, customer, conversation = _context(fake_store)
    first = _menu(fake_store, business, "V1")
    client = _whatsapp()

    assert await menu_image.send_menu_image(client, business, customer, conversation, auto=True)
    assert not await menu_image.send_menu_image(client, business, customer, conversation, auto=True)
    client.send_image.assert_awaited_once_with(customer.phone_number, "V1")

    # El admin publica otro menú a mitad de sesión: el nuevo sale una vez.
    first.status = DailyMenuStatus.REEMPLAZADO.value
    _menu(fake_store, business, "V2")
    assert await menu_image.send_menu_image(client, business, customer, conversation, auto=True)
    assert client.send_image.await_args.args == (customer.phone_number, "V2")
    assert not await menu_image.send_menu_image(client, business, customer, conversation, auto=True)


async def test_tenant_can_turn_off_the_automatic_send(fake_store):
    business, customer, conversation = _context(fake_store, enviar_menu_al_iniciar=False)
    _menu(fake_store, business, "V1")
    client = _whatsapp()

    assert not await menu_image.send_menu_image(client, business, customer, conversation, auto=True)
    # Si el cliente la pide, sí sale.
    assert await menu_image.send_menu_image(client, business, customer, conversation, auto=False)


async def test_first_flush_of_the_day_sends_the_image_before_the_reply(fake_store):
    """Caso real: "quiero ordenar" con menú publicado respondía el menú en
    texto. Ahora la foto sale sola y el modelo recibe la nota."""
    business, customer, conversation = _context(fake_store)
    _menu(fake_store, business, "V1")
    message = Messages()
    message.business_id = business.bussiness_id
    message.customer_id = customer.customer_id
    message.conversation_id = conversation.conversation_id
    message.content = "quiero ordenar"
    message.type = "text"
    message.created_at = datetime.now(timezone.utc) - timedelta(seconds=30)  # el cliente ya dejó de escribir
    fake_store.seed(message)

    order = []
    client = _whatsapp()
    client.send_image.side_effect = lambda *a: order.append("imagen") or True
    get_reply = AsyncMock(side_effect=lambda **kw: order.append("ia") or AgentReply(text="¿Qué te provoca?"))

    with patch.object(inbound_pipeline.WhatsAppClient, "for_business", return_value=client), patch.object(
        inbound_pipeline, "get_vertical_handlers", return_value=DummyHandlers("cliente", customer.customer_id)
    ), patch.object(inbound_pipeline._agent_runner, "get_reply", new=get_reply), patch(
        "app.services.messaging.inbound_pipeline.OutboundMessageService.send_and_persist", new=AsyncMock()
    ):
        await inbound_pipeline.flush_conversation(
            business_id=business.bussiness_id,
            customer_id=customer.customer_id,
            conversation_id=conversation.conversation_id,
        )

    assert order == ["imagen", "ia"]
    text = get_reply.await_args.kwargs["message_text"]
    assert text.startswith(inbound_pipeline._MENU_SENT_NOTE) and text.endswith("quiero ordenar")
    assert conversation.menu_enviado.startswith(_today())


async def test_catalog_link_is_sent_as_text_instead_of_images(fake_store):
    business, customer, conversation = _context(fake_store, carta_media_id="M1")
    carta = DailyMenus()
    carta.business_id = business.bussiness_id
    carta.status = DailyMenuStatus.CARTA_ACTIVA.value
    carta.extracted_menu = {"media_ids": ["M1"], "tipo": "catalogo", "link": "https://wa.me/c/57300"}
    fake_store.seed(carta)
    client = _whatsapp()
    client.send_message = AsyncMock(return_value=True)

    assert await menu_image.send_menu_image(client, business, customer, conversation, auto=False)

    client.send_image.assert_not_called()
    assert "https://wa.me/c/57300" in client.send_message.call_args.args[1]
