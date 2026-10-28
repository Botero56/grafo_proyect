from unittest.mock import AsyncMock, patch

from app.ai.ai_restaurant.user.tools import customer_tools
from app.ai.ai_restaurant.user.tools.customer_tools import send_menu_image
from tests.conftest import (
    FakeToolContext,
    seed_business,
    seed_configuration,
    seed_conversation,
    seed_customer,
)


def _tool_context(fake_store, **config):
    business = seed_business(fake_store)
    customer = seed_customer(fake_store, business)
    conversation = seed_conversation(fake_store, business, customer.customer_id)
    seed_configuration(fake_store, business, **config)
    return customer, FakeToolContext(business.bussiness_id, customer.customer_id, conversation.conversation_id)


async def test_resends_the_image_when_the_customer_asks_again(fake_store):
    customer, ctx = _tool_context(fake_store, carta_media_id="MEDIA123")

    with patch.object(customer_tools.WhatsAppClient, "send_image", new=AsyncMock(return_value=True)) as send:
        assert await send_menu_image(ctx) == {"sent": True}
        # A pedido del cliente sale aunque ya la tenga (no es el envío automático).
        assert await send_menu_image(ctx) == {"sent": True}

    assert send.await_count == 2
    send.assert_awaited_with(customer.phone_number, "MEDIA123")


async def test_without_image_today_the_agent_falls_back_to_text(fake_store):
    _, ctx = _tool_context(fake_store)

    with patch.object(customer_tools.WhatsAppClient, "send_image", new=AsyncMock()) as send:
        assert await send_menu_image(ctx) == {"sent": False}

    send.assert_not_awaited()
