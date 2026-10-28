import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.ai.core.agent_runner import AgentReply
from app.config import handoff_settings
from app.firestore_db.firebase_schemas import Messages
from tests.conftest import (
    DummyHandlers,
    ingest_and_reply,
    seed_business,
    seed_configuration,
    seed_conversation,
    seed_customer,
)

from app.services.messaging import inbound_pipeline


def _build_context(
    fake_store,
    *,
    bot_activo: bool,
    motivo_handoff: str | None = None,
    ultimo_mensaje_humano_at: datetime | None = None,
    pausado_desde: datetime | None = None,
):
    business = seed_business(fake_store)

    customer = seed_customer(fake_store, business, name="Cliente Test")
    conversation = seed_conversation(
        fake_store,
        business,
        customer.customer_id,
        bot_activo=bot_activo,
        motivo_handoff=motivo_handoff,
        ultimo_mensaje_humano_at=ultimo_mensaje_humano_at,
        pausado_desde=pausado_desde,
    )

    message = Messages()
    message.business_id = business.bussiness_id
    message.customer_id = customer.customer_id
    message.conversation_id = conversation.conversation_id
    message.content = "Hola, quiero hablar con alguien"
    message.type = "text"
    message.created_at = datetime.now(timezone.utc) - timedelta(seconds=30)  # el cliente ya dejó de escribir
    fake_store.seed(message)

    return {
        "business": business,
        "customer": customer,
        "conversation": conversation,
        "message": message,
        "message_delay_seconds": None,
    }


@pytest.mark.asyncio
async def test_customer_message_while_bot_paused_never_calls_ai(fake_store):
    # Dentro de la ventana de HANDOFF_QUICK_REACTIVATION_SECONDS (la
    # encargada acaba de responder) -- todavía debe quedar pausado.
    result = _build_context(
        fake_store,
        bot_activo=False,
        motivo_handoff="respuesta_manual",
        ultimo_mensaje_humano_at=datetime.now(timezone.utc),
    )

    with patch.object(
        inbound_pipeline._handoff_service, "notify_human", new=AsyncMock(return_value=True)
    ) as notify_mock, patch.object(
        inbound_pipeline, "get_vertical_handlers", side_effect=AssertionError("no debería llamarse")
    ):
        await inbound_pipeline._process_customer_message(result)

    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["motivo"] == "respuesta_manual"
    assert result["conversation"].bot_activo is False


@pytest.mark.asyncio
async def test_courier_settlement_is_handled_even_while_the_bot_is_paused(fake_store):
    """Prueba real 2026-10-16: una respuesta de la encargada pausó el chat
    del domiciliario y su "liquidar 4" ya no se registraba."""
    from app.services.delivery import couriers

    result = _build_context(
        fake_store, bot_activo=False, motivo_handoff="respuesta_manual",
        ultimo_mensaje_humano_at=datetime.now(timezone.utc),
    )
    seed_configuration(fake_store, result["business"], couriers=[{"phone": result["customer"].phone_number, "name": "Carlos"}])
    result["message"].content = "liquidar 4"

    with patch.object(
        inbound_pipeline._handoff_service, "notify_human", new=AsyncMock(return_value=True)
    ) as notify_mock, patch.object(couriers, "OutboundMessageService") as outbound:
        outbound.return_value.send_and_persist = AsyncMock()
        assert await inbound_pipeline._process_customer_message(result) is None

    notify_mock.assert_not_awaited()
    assert "No existen hoy: #4" in outbound.return_value.send_and_persist.await_args.kwargs["text"]
    assert result["message"].status == "completed"
    assert result["conversation"].bot_activo is False  # lo conversacional sigue con el humano


@pytest.mark.asyncio
async def test_customer_message_auto_reactivates_after_quick_inactivity_window(fake_store):
    # Ya pasó HANDOFF_QUICK_REACTIVATION_SECONDS desde el último mensaje de
    # la encargada -- este mensaje del cliente debe reactivar el bot solo,
    # sin que nadie lo autorice desde el chat admin, y la IA debe responder
    # con normalidad.
    stale = datetime.now(timezone.utc) - timedelta(seconds=handoff_settings.HANDOFF_QUICK_REACTIVATION_SECONDS + 5)
    result = _build_context(
        fake_store,
        bot_activo=False,
        motivo_handoff="respuesta_manual",
        ultimo_mensaje_humano_at=stale,
    )

    fake_reply = AgentReply(text="¡Hola! ¿en qué te ayudo?")

    with patch.object(
        inbound_pipeline,
        "get_vertical_handlers",
        return_value=DummyHandlers("cliente", result["customer"].customer_id),
    ), patch.object(
        inbound_pipeline._agent_runner, "get_reply", new=AsyncMock(return_value=fake_reply)
    ), patch(
        "app.services.messaging.inbound_pipeline.OutboundMessageService.send_and_persist",
        new=AsyncMock(return_value=result["message"]),
    ) as send_mock, patch.object(
        inbound_pipeline._handoff_service, "notify_human", new=AsyncMock(return_value=True)
    ) as notify_mock:
        await ingest_and_reply(result)

    conversation = result["conversation"]
    assert conversation.bot_activo is True
    assert conversation.motivo_handoff is None
    notify_mock.assert_not_awaited()
    send_mock.assert_awaited_once()
    assert send_mock.await_args.kwargs["text"] == fake_reply.text


@pytest.mark.asyncio
async def test_ai_requires_human_does_not_pause_bot_but_notifies_and_sends_configured_message(fake_store):
    result = _build_context(fake_store, bot_activo=True)

    fake_reply = AgentReply(
        text="dejame revisar...",
        requires_human=True,
        handoff_reason="precio_variable",
        handoff_summary="Reclamo por un pedido incompleto.",
    )

    with patch.object(
        inbound_pipeline,
        "get_vertical_handlers",
        return_value=DummyHandlers("cliente", result["customer"].customer_id),
    ), patch.object(
        inbound_pipeline._agent_runner, "get_reply", new=AsyncMock(return_value=fake_reply)
    ), patch(
        "app.services.messaging.inbound_pipeline.OutboundMessageService.send_and_persist",
        new=AsyncMock(return_value=result["message"]),
    ) as send_mock, patch.object(
        inbound_pipeline._handoff_service, "notify_handoff", new=AsyncMock(return_value=True)
    ) as notify_mock:
        await ingest_and_reply(result)

    conversation = result["conversation"]
    # El bot NO se pausa por un pedido de handoff de la IA -- sigue
    # atendiendo normalmente; solo un echo real de la encargada pausa la
    # conversación (ver _process_echo_context).
    assert conversation.bot_activo is True

    send_mock.assert_awaited_once()
    sent_text = send_mock.await_args.kwargs["text"]
    assert sent_text  # el mensaje de traspaso configurado, no el texto libre del modelo
    assert sent_text != fake_reply.text

    notify_mock.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("motivo", ["baja_confianza", None, "coordinar_pago"])
async def test_a_handoff_without_a_valid_reason_sends_the_normal_reply(fake_store, motivo):
    """Regresión (prueba real 2026-10-05): la IA pedía un humano por "baja
    confianza" ante un pago mixto y el cliente recibía "te atiende alguien
    de nuestro equipo" sin haberlo pedido. Sin un motivo válido no hay
    traspaso: sale la respuesta de la IA y no se molesta al admin."""
    result = _build_context(fake_store, bot_activo=True)
    fake_reply = AgentReply(
        text="Claro, ¿la sopa la quieres de frijoles?",
        requires_human=True,
        handoff_reason=motivo,
        handoff_summary="Reclamo por un pedido incompleto.",
    )

    with patch.object(
        inbound_pipeline,
        "get_vertical_handlers",
        return_value=DummyHandlers("cliente", result["customer"].customer_id),
    ), patch.object(
        inbound_pipeline._agent_runner, "get_reply", new=AsyncMock(return_value=fake_reply)
    ), patch(
        "app.services.messaging.inbound_pipeline.OutboundMessageService.send_and_persist",
        new=AsyncMock(return_value=result["message"]),
    ) as send_mock, patch.object(
        inbound_pipeline._handoff_service, "notify_handoff", new=AsyncMock(return_value=True)
    ) as notify_mock:
        await ingest_and_reply(result)

    assert send_mock.await_args.kwargs["text"] == fake_reply.text
    notify_mock.assert_not_awaited()
