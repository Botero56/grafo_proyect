"""Barrio corto o parecido a varias zonas ("Belén"): el cliente elige en una
lista de WhatsApp y el código pone la tarifa (nunca la IA). "Ninguno de
estos" pasa a cotización con el equipo."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.sessions import InMemorySessionService

from app.ai.ai_restaurant.user.tools import customer_tools
from app.ai.ai_restaurant.user.tools.order_tools import render_order_state, update_order
from app.ai.core.agent_runner import AgentRunnerService
from app.firestore_db.firebase_schemas import Conversations, Messages
from app.schemas.enum_schemas import DeliveryStatus
from app.services.delivery import quotes, zone_choice
from app.services.delivery.zones import candidate_zones
from app.services.firestore_services.firestore_webhook_service import WhatsAppWebhookService
from tests.conftest import FakeToolContext, seed_active_menu, seed_business, seed_configuration, seed_conversation, seed_customer

ZONES = [
    {"nombre": "Belén Rincón", "tarifa": 5000, "alias": []},
    {"nombre": "Belén Las Playas", "tarifa": 4000, "alias": []},
    {"nombre": "Laureles", "tarifa": 3000, "alias": []},
]
MENU = {"accion": "agregar", "producto": "menu_del_dia", "elecciones": {"Sopa": "Frijoles"}}
ENTREGA = {"accion": "entrega", "direccion": "Calle 30 # 80-10", "barrio": "Belén"}


def test_candidates_are_zones_that_contain_or_look_like_the_barrio():
    assert [z["nombre"] for z in candidate_zones("Belén", ZONES)] == ["Belén Rincón", "Belén Las Playas"]
    assert [z["nombre"] for z in candidate_zones("laurels", ZONES)] == ["Laureles"]  # tipeo
    assert candidate_zones("Robledo", ZONES) == []
    assert candidate_zones("be", ZONES) == []


def test_a_list_reply_is_read_like_a_button():
    content = WhatsAppWebhookService._extract_message_content(
        None, "interactive", {"interactive": {"type": "list_reply", "list_reply": {"id": "zona:1", "title": "Belén"}}}
    )
    assert content[0] == "zona:1"


@pytest.fixture
def setup(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, delivery_zones=ZONES)
    seed_active_menu(fake_store, business)
    customer = seed_customer(fake_store, business, name="Ana")
    conversation = seed_conversation(fake_store, business, customer.customer_id)
    ctx = FakeToolContext(business.bussiness_id, customer.customer_id, conversation.conversation_id)
    return business, customer, conversation, ctx


async def test_an_ambiguous_barrio_sends_the_list_instead_of_asking_the_admin(setup):
    _, _, _, ctx = setup
    with patch.object(customer_tools, "ask_zone_choice", AsyncMock(return_value=True)) as ask, \
         patch.object(quotes, "request_quote", AsyncMock(return_value=True)) as quote:
        result = await update_order([MENU, ENTREGA], ctx)
        await update_order([ENTREGA], ctx)  # la IA repite la dirección: no se reenvía

    ask.assert_awaited_once()
    quote.assert_not_awaited()
    assert [z["nombre"] for z in ask.await_args.args[0]] == ["Belén Rincón", "Belén Las Playas"]
    assert any("lista de barrios" in f for f in result["falta"])


async def test_a_barrio_like_no_zone_still_goes_to_the_admin(setup):
    _, _, _, ctx = setup
    with patch.object(customer_tools, "ask_zone_choice", AsyncMock(return_value=True)) as ask, \
         patch.object(quotes, "request_quote", AsyncMock(return_value=True)) as quote:
        await update_order([MENU, {**ENTREGA, "barrio": "Robledo"}], ctx)

    ask.assert_not_awaited()
    quote.assert_awaited_once()


@pytest.fixture
def runner():
    service = AgentRunnerService()
    service._session_service = InMemorySessionService()
    with patch.object(zone_choice, "shared_agent_runner", return_value=service):
        yield service


async def _waiting_for_choice(setup, runner):
    """Sesión con el pedido esperando que el cliente elija en la lista."""
    business, customer, conversation, ctx = setup
    with patch.object(customer_tools, "ask_zone_choice", AsyncMock(return_value=True)):
        await update_order([MENU, ENTREGA], ctx)
    await runner.edit_state(business.bussiness_id, conversation.conversation_id, customer.customer_id,
                            lambda state: ({**ctx.state}, None))


def _reply(conversation, content):
    message = Messages(conversation_id=conversation.conversation_id, type="interactive", content=content)
    message.save()
    return message


async def _state(runner, business, customer, conversation):
    return await runner.edit_state(business.bussiness_id, conversation.conversation_id, customer.customer_id,
                                   lambda state: (None, state))


async def test_choosing_a_zone_sets_its_fee_and_the_agent_gets_a_note(setup, runner):
    business, customer, conversation, _ = setup
    await _waiting_for_choice(setup, runner)
    message = _reply(conversation, "zona:1")

    assert await zone_choice.maybe_handle(business, customer, conversation, message) is False

    stored = Conversations.collection.get(conversation.conversation_id)
    assert (stored.delivery_zone, stored.delivery_fee, stored.delivery_status) == ("Belén Las Playas", 4000, DeliveryStatus.ZONA.value)
    state = await _state(runner, business, customer, conversation)
    assert state["pedido"]["domicilio"] == 4000 and not state["pedido"]["zona_opciones"]
    assert "lista de barrios" not in render_order_state(state) and state["pedido"]["total"] == 18500 + 4000
    assert "Belén Las Playas" in message.content and "$4.000" in message.content


async def test_none_of_these_asks_the_admin_for_a_quote(setup, runner):
    business, customer, conversation, _ = setup
    await _waiting_for_choice(setup, runner)
    message = _reply(conversation, "zona:ninguna")

    with patch.object(quotes, "request_quote", AsyncMock(return_value=True)) as quote:
        await zone_choice.maybe_handle(business, customer, conversation, message)

    quote.assert_awaited_once()
    assert "cotizando" in message.content


async def test_an_old_list_does_nothing(setup, runner):
    business, customer, conversation, _ = setup
    message = _reply(conversation, "zona:0")

    await zone_choice.maybe_handle(business, customer, conversation, message)

    assert Conversations.collection.get(conversation.conversation_id).delivery_zone is None
    assert "ya no aplica" in message.content


async def test_other_messages_are_not_touched(setup, runner):
    business, customer, conversation, _ = setup
    message = _reply(conversation, "propina:abc:si")
    assert await zone_choice.maybe_handle(business, customer, conversation, message) is False
    assert message.content == "propina:abc:si"
