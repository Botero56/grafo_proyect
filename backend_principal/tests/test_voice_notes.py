"""Notas de voz: se transcriben antes de que la IA razone y quedan como
texto en Messages, así una corrección por audio pesa igual que una escrita.
Antes viajaba crudo solo el último audio de la tanda y en la sesión quedaba
"[audio del cliente, ya respondido]"."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.ai.core.agent_runner import AgentReply
from app.ai.core.audio_processor import AudioProcessingError, ProcessedAudio
from app.firestore_db.firebase_schemas import Messages
from app.services.messaging import inbound_pipeline
from tests.conftest import DummyHandlers, seed_business, seed_conversation, seed_customer

_BASE = datetime.now(timezone.utc) - timedelta(seconds=30)


def _message(fake_store, business, customer, conversation, offset, *, text=None, audio=None):
    message = Messages()
    message.business_id = business.bussiness_id
    message.customer_id = customer.customer_id
    message.conversation_id = conversation.conversation_id
    message.type = "audio" if audio else "text"
    message.media_id = audio
    message.content = inbound_pipeline._AUDIO_PLACEHOLDER if audio else text
    message.created_at = _BASE + timedelta(seconds=offset)
    return fake_store.seed(message)


async def test_every_voice_note_in_the_burst_is_transcribed_and_saved(fake_store):
    business = seed_business(fake_store)
    customer = seed_customer(fake_store, business)
    conversation = seed_conversation(fake_store, business, customer.customer_id, bot_activo=True)
    _message(fake_store, business, customer, conversation, 0, text="quiero una carne")
    first = _message(fake_store, business, customer, conversation, 1, audio="A1")
    second = _message(fake_store, business, customer, conversation, 2, audio="A2")

    transcripts = {"A1": "no no espera, no es correcto", "A2": "mejor un pollo"}
    process = AsyncMock(side_effect=lambda media_id, mime: ProcessedAudio("audio/ogg", 3.0, media_id.encode()))
    transcribe = AsyncMock(side_effect=lambda audio: transcripts[audio.data.decode()])
    get_reply = AsyncMock(return_value=AgentReply(text="Listo, un pollo."))

    with patch.object(inbound_pipeline.AudioProcessor, "process", new=process), patch.object(
        inbound_pipeline, "transcribe", new=transcribe
    ), patch.object(
        inbound_pipeline, "get_vertical_handlers", return_value=DummyHandlers("cliente", customer.customer_id)
    ), patch.object(inbound_pipeline._agent_runner, "get_reply", new=get_reply), patch(
        "app.services.messaging.inbound_pipeline.OutboundMessageService.send_and_persist", new=AsyncMock()
    ):
        await inbound_pipeline.flush_conversation(
            business_id=business.bussiness_id,
            customer_id=customer.customer_id,
            conversation_id=conversation.conversation_id,
        )

    assert get_reply.await_args.kwargs["message_text"] == (
        "quiero una carne\n(nota de voz) no no espera, no es correcto\n(nota de voz) mejor un pollo"
    )
    assert "audio_bytes" not in get_reply.await_args.kwargs
    assert (first.content, second.content) == ("no no espera, no es correcto", "mejor un pollo")


async def test_an_unreadable_voice_note_asks_the_ai_to_get_it_repeated(fake_store):
    business = seed_business(fake_store)
    customer = seed_customer(fake_store, business)
    conversation = seed_conversation(fake_store, business, customer.customer_id, bot_activo=True)
    note = _message(fake_store, business, customer, conversation, 0, audio="A1")
    get_reply = AsyncMock(return_value=AgentReply(text="¿Me lo repites?"))

    with patch.object(
        inbound_pipeline.AudioProcessor, "process", new=AsyncMock(side_effect=AudioProcessingError("muy largo"))
    ), patch.object(
        inbound_pipeline, "get_vertical_handlers", return_value=DummyHandlers("cliente", customer.customer_id)
    ), patch.object(inbound_pipeline._agent_runner, "get_reply", new=get_reply), patch(
        "app.services.messaging.inbound_pipeline.OutboundMessageService.send_and_persist", new=AsyncMock()
    ):
        await inbound_pipeline.flush_conversation(
            business_id=business.bussiness_id,
            customer_id=customer.customer_id,
            conversation_id=conversation.conversation_id,
        )

    assert note.content == inbound_pipeline._UNHEARD_AUDIO
    assert get_reply.await_args.kwargs["message_text"] == f"(nota de voz) {inbound_pipeline._UNHEARD_AUDIO}"
