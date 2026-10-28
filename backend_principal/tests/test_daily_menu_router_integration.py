from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.ai.core.agent_runner import AgentReply
from app.firestore_db.firebase_schemas import Messages
from tests.conftest import (
    DummyHandlers,
    ingest_and_reply,
    seed_business,
    seed_conversation,
    seed_customer,
)

from app.services.messaging import inbound_pipeline


def _build_context(fake_store, business, phone):
    customer = seed_customer(fake_store, business, phone)
    conversation = seed_conversation(fake_store, business, customer.customer_id, bot_activo=True)

    message = Messages()
    message.business_id = business.bussiness_id
    message.customer_id = customer.customer_id
    message.conversation_id = conversation.conversation_id
    message.created_at = datetime.now(timezone.utc) - timedelta(seconds=30)  # el cliente ya dejó de escribir
    message.type = "image"
    message.media_id = "media_1"
    message.content = ""
    fake_store.seed(message)

    return {
        "business": business,
        "customer": customer,
        "conversation": conversation,
        "message": message,
        "message_delay_seconds": None,
    }


@pytest.mark.asyncio
async def test_unauthorized_number_image_never_reaches_menu_orchestrator(fake_store):
    business = seed_business(fake_store)
    result = _build_context(fake_store, business, "573009999999")

    fake_reply = AgentReply(text="hola, ¿en qué te ayudo?")

    with patch.object(
        inbound_pipeline,
        "get_vertical_handlers",
        return_value=DummyHandlers("cliente", result["customer"].customer_id),
    ), patch.object(
        inbound_pipeline._agent_runner, "get_reply", new=AsyncMock(return_value=fake_reply)
    ), patch(
        "app.services.messaging.inbound_pipeline.OutboundMessageService.send_and_persist",
        new=AsyncMock(return_value=result["message"]),
    ), patch(
        "app.services.messaging.inbound_pipeline.MenuIngestionOrchestrator"
    ) as OrchestratorCls:
        await ingest_and_reply(result)

    OrchestratorCls.assert_not_called()


@pytest.mark.asyncio
async def test_authorized_admin_image_is_intercepted_before_the_agent(fake_store):
    business = seed_business(fake_store)
    result = _build_context(fake_store, business, "573001111111")

    with patch.object(
        inbound_pipeline, "get_vertical_handlers", return_value=DummyHandlers("admin", None)
    ), patch.object(
        inbound_pipeline._agent_runner,
        "get_reply",
        new=AsyncMock(side_effect=AssertionError("no debería llamarse al agente")),
    ), patch("app.services.messaging.inbound_pipeline.MenuIngestionOrchestrator") as OrchestratorCls:
        instance = OrchestratorCls.return_value
        instance.maybe_handle = AsyncMock(return_value=True)

        await inbound_pipeline._process_customer_message(result)

    OrchestratorCls.assert_called_once()
    instance.maybe_handle.assert_awaited_once_with(result["business"], result["customer"], result["message"])
