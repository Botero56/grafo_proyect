"""La marca de agua (`Conversations.ultimo_mensaje_respondido_at`) es lo que
hace seguro que sobren tareas de flush: cada mensaje agenda la suya, y solo
la primera que corre contesta. Si esto se rompe, el cliente recibe la
respuesta duplicada."""

from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta, timezone

import pytest

from app.firestore_db.firebase_schemas import Messages
from app.schemas.enum_schemas import MessageDirection, MessageOrigin, MessageStatus
from tests.conftest import DummyHandlers, seed_business, seed_conversation, seed_customer

from app.services.messaging import inbound_pipeline

# Relativo a "ahora": el claim descarta lo que tenga más de _CLAIM_MAX_AGE,
# así que una fecha fija en el calendario haría que estos tests empezaran a
# fallar solos con el paso del tiempo.
_BASE = datetime.now(timezone.utc) - timedelta(seconds=30)


def _context(fake_store):
    business = seed_business(fake_store)

    customer = seed_customer(fake_store, business, name="Cliente Test")
    conversation = seed_conversation(fake_store, business, customer.customer_id, bot_activo=True)

    return business, customer, conversation


def _incoming(fake_store, business, customer, conversation, text, offset_seconds, *, tagged=False):
    """Por defecto se crea como lo hace la ingesta REAL: sin `direction` ni
    `origin`. Esa fidelidad importa -- los primeros tests seteaban
    direction="entrante" y pasaban mientras en producción el flush no
    encontraba nada (2026-09-24). `tagged=True` cubre el día que la ingesta
    sí empiece a etiquetarlos."""
    message = Messages()
    message.business_id = business.bussiness_id
    message.customer_id = customer.customer_id
    message.conversation_id = conversation.conversation_id
    message.content = text
    message.type = "text"
    if tagged:
        message.direction = MessageDirection.ENTRANTE.value
        message.origin = MessageOrigin.CLIENTE.value
    message.created_at = _BASE + timedelta(seconds=offset_seconds)
    return fake_store.seed(message)


def test_claim_takes_every_unanswered_message_in_order(fake_store):
    business, customer, conversation = _context(fake_store)
    _incoming(fake_store, business, customer, conversation, "hola", 0)
    _incoming(fake_store, business, customer, conversation, "ya llegue", 2)
    _incoming(fake_store, business, customer, conversation, "que hay", 4)

    pending = inbound_pipeline._claim_pending_messages(conversation)

    assert [m.content for m in pending] == ["hola", "ya llegue", "que hay"]
    assert conversation.ultimo_mensaje_respondido_at == _BASE + timedelta(seconds=4)


def test_second_claim_finds_nothing_so_the_reply_is_never_duplicated(fake_store):
    business, customer, conversation = _context(fake_store)
    _incoming(fake_store, business, customer, conversation, "hola", 0)

    first = inbound_pipeline._claim_pending_messages(conversation)
    second = inbound_pipeline._claim_pending_messages(conversation)

    assert [m.content for m in first] == ["hola"]
    assert second == []


def test_a_message_arriving_after_the_claim_is_picked_up_by_the_next_flush(fake_store):
    business, customer, conversation = _context(fake_store)
    _incoming(fake_store, business, customer, conversation, "hola", 0)
    inbound_pipeline._claim_pending_messages(conversation)

    _incoming(fake_store, business, customer, conversation, "otra cosa", 10)
    pending = inbound_pipeline._claim_pending_messages(conversation)

    assert [m.content for m in pending] == ["otra cosa"]


def test_messages_tagged_as_entrante_are_also_claimed(fake_store):
    """Hoy la ingesta no etiqueta; si algun dia lo hace, el claim tiene que
    seguir funcionando."""
    business, customer, conversation = _context(fake_store)
    _incoming(fake_store, business, customer, conversation, "etiquetado", 0, tagged=True)

    pending = inbound_pipeline._claim_pending_messages(conversation)

    assert [m.content for m in pending] == ["etiquetado"]


def test_human_echo_is_never_claimed(fake_store):
    """Un echo (la encargada escribiendo desde el movil) es saliente aunque
    no lo haya generado la IA: la IA no debe 'contestarlo'."""
    business, customer, conversation = _context(fake_store)
    echo = Messages()
    echo.business_id = business.bussiness_id
    echo.customer_id = customer.customer_id
    echo.conversation_id = conversation.conversation_id
    echo.content = "te escribo yo, la encargada"
    echo.direction = MessageDirection.SALIENTE.value
    echo.origin = MessageOrigin.HUMANO.value
    echo.created_at = _BASE
    fake_store.seed(echo)

    assert inbound_pipeline._claim_pending_messages(conversation) == []


def test_outgoing_messages_are_never_claimed(fake_store):
    business, customer, conversation = _context(fake_store)
    reply = Messages()
    reply.business_id = business.bussiness_id
    reply.customer_id = customer.customer_id
    reply.conversation_id = conversation.conversation_id
    reply.content = "respuesta de la IA"
    reply.direction = MessageDirection.SALIENTE.value
    reply.origin = MessageOrigin.IA.value
    reply.created_at = _BASE
    fake_store.seed(reply)

    assert inbound_pipeline._claim_pending_messages(conversation) == []


def test_messages_of_another_conversation_are_not_claimed(fake_store):
    business, customer, conversation = _context(fake_store)
    other = seed_conversation(fake_store, business, customer.customer_id)

    _incoming(fake_store, business, customer, other, "de otra charla", 0)

    assert inbound_pipeline._claim_pending_messages(conversation) == []


def test_messages_older_than_the_window_are_not_claimed(fake_store):
    """Una conversación sin marca de agua (todas las anteriores a este
    campo) no debe hacer que el primer flush le pase a la IA el historial
    entero como si el cliente lo acabara de escribir."""
    business, customer, conversation = _context(fake_store)
    _incoming(fake_store, business, customer, conversation, "de hace rato", -3600)
    _incoming(fake_store, business, customer, conversation, "de ayer", -86400)
    _incoming(fake_store, business, customer, conversation, "recien", 0)

    pending = inbound_pipeline._claim_pending_messages(conversation)

    assert [m.content for m in pending] == ["recien"]


def test_a_burst_is_capped_to_the_most_recent_messages(fake_store):
    business, customer, conversation = _context(fake_store)
    total = inbound_pipeline._CLAIM_MAX_MESSAGES + 5
    for i in range(total):
        _incoming(fake_store, business, customer, conversation, f"msg {i}", i)

    pending = inbound_pipeline._claim_pending_messages(conversation)

    assert len(pending) == inbound_pipeline._CLAIM_MAX_MESSAGES
    assert pending[-1].content == f"msg {total - 1}"
    # Lo que quedó fuera del tope no se vuelve a mirar.
    assert inbound_pipeline._claim_pending_messages(conversation) == []


def test_naive_timestamps_do_not_break_the_comparison(fake_store):
    """Según cómo se escribieron, Firestore puede devolver fechas sin
    tzinfo; compararlas con una fecha aware revienta con TypeError."""
    business, customer, conversation = _context(fake_store)
    message = _incoming(fake_store, business, customer, conversation, "sin tz", 0)
    message.created_at = message.created_at.replace(tzinfo=None)

    pending = inbound_pipeline._claim_pending_messages(conversation)

    assert [m.content for m in pending] == ["sin tz"]


@pytest.mark.asyncio
async def test_flush_without_pending_messages_does_not_call_the_agent(fake_store):
    business, customer, conversation = _context(fake_store)

    answered = await inbound_pipeline.flush_conversation(
        business_id=business.bussiness_id,
        customer_id=customer.customer_id,
        conversation_id=conversation.conversation_id,
    )

    assert answered is False


@pytest.mark.asyncio
async def test_flush_with_missing_context_is_a_no_op(fake_store):
    answered = await inbound_pipeline.flush_conversation(
        business_id="no-existe",
        customer_id="no-existe",
        conversation_id="no-existe",
    )

    assert answered is False


def test_messages_already_handled_outside_the_ai_are_not_claimed(fake_store):
    business, customer, conversation = _context(fake_store)
    handled = _incoming(fake_store, business, customer, conversation, "menu:abc:confirm", 0)
    handled.status = MessageStatus.COMPLETED.value
    _incoming(fake_store, business, customer, conversation, "Mándame el reporte", 2)

    pending = inbound_pipeline._claim_pending_messages(conversation)

    assert [m.content for m in pending] == ["Mándame el reporte"]


@pytest.mark.asyncio
async def test_admin_menu_messages_do_not_reach_the_ai_with_the_next_request(fake_store):
    """Regresión (prueba real 2026-09-30): la IA del admin recibía
    "[Imagen recibida] | menu:…:correct | … | Mándame el reporte" como un
    solo mensaje, porque el flush se llevaba también lo que ya había
    atendido el flujo del menú."""
    business, admin, conversation = _context(fake_store)
    image = _incoming(fake_store, business, admin, conversation, "[Imagen recibida]", 0)
    image.type = "image"
    report = _incoming(fake_store, business, admin, conversation, "Mándame el reporte", 2)

    def context_for(message):
        return {"business": business, "customer": admin, "conversation": conversation,
                "message": message, "message_delay_seconds": None}

    with patch.object(
        inbound_pipeline, "get_vertical_handlers", return_value=DummyHandlers("admin")
    ), patch("app.services.messaging.inbound_pipeline.MenuIngestionOrchestrator") as orchestrator:
        orchestrator.return_value.maybe_handle = AsyncMock(side_effect=lambda b, c, m: m is image)
        assert await inbound_pipeline._process_customer_message(context_for(image)) is None
        assert await inbound_pipeline._process_customer_message(context_for(report))

    pending = inbound_pipeline._claim_pending_messages(conversation)

    assert [m.content for m in pending] == ["Mándame el reporte"]


@pytest.mark.asyncio
async def test_a_failure_before_the_ai_gives_the_messages_back_for_the_retry(fake_store):
    """Regresión: la marca de agua avanza antes de contestar. Si algo fallaba
    antes de invocar a la IA, esos mensajes quedaban "contestados" y nadie
    los respondía. Ahora se devuelven y el worker pide el reintento."""
    business, customer, conversation = _context(fake_store)
    _incoming(fake_store, business, customer, conversation, "hola", 0)

    class _BrokenHandlers(DummyHandlers):
        def resolve_role(self, **kwargs):
            raise RuntimeError("firestore caído")

    with patch.object(inbound_pipeline, "get_vertical_handlers", return_value=_BrokenHandlers("cliente")):
        with pytest.raises(inbound_pipeline.RetryableTaskError):
            await inbound_pipeline.flush_conversation(
                business_id=business.bussiness_id,
                customer_id=customer.customer_id,
                conversation_id=conversation.conversation_id,
            )

    assert [m.content for m in inbound_pipeline._claim_pending_messages(conversation)] == ["hola"]


@pytest.mark.asyncio
async def test_a_message_without_content_does_not_reach_the_ai(fake_store):
    """Regresión (prueba real 2026-10-05): WhatsApp manda "unsupported" sin
    contenido (ej. varias fotos juntas) y la IA contestaba algo fuera de
    contexto a un mensaje vacío."""
    business, customer, conversation = _context(fake_store)
    empty = _incoming(fake_store, business, customer, conversation, "", 0)
    empty.type = "unsupported"

    with patch.object(
        inbound_pipeline, "get_vertical_handlers", return_value=DummyHandlers("cliente")
    ), patch.object(inbound_pipeline._agent_runner, "get_reply", new=AsyncMock()) as get_reply:
        await inbound_pipeline.flush_conversation(
            business_id=business.bussiness_id,
            customer_id=customer.customer_id,
            conversation_id=conversation.conversation_id,
        )

    get_reply.assert_not_awaited()


def test_a_customer_still_typing_postpones_the_reply():
    """Ventana deslizante con tope: se espera a que el cliente pare, pero
    una ráfaga que ya lleva el tope esperando se contesta igual."""
    now = datetime.now(timezone.utc)

    def at(seconds_ago):
        message = Messages()
        message.created_at = now - timedelta(seconds=seconds_ago)
        return message

    assert inbound_pipeline._seconds_until_quiet([]) == 0
    assert 3.5 < inbound_pipeline._seconds_until_quiet([at(4), at(1)]) <= 4
    assert inbound_pipeline._seconds_until_quiet([at(9), at(6)]) == 0
    assert inbound_pipeline._seconds_until_quiet([at(25), at(0)]) == 0


@pytest.mark.asyncio
async def test_postponed_flush_reschedules_itself_and_claims_nothing(fake_store):
    business, customer, conversation = _context(fake_store)
    message = _incoming(fake_store, business, customer, conversation, "y una gaseosa", 0)
    message.created_at = datetime.now(timezone.utc)

    with patch.object(inbound_pipeline.cloud_tasks_client, "enqueue_flush") as enqueue:
        answered = await inbound_pipeline.flush_conversation(
            business_id=business.bussiness_id,
            customer_id=customer.customer_id,
            conversation_id=conversation.conversation_id,
        )

    assert answered is False
    assert enqueue.call_args.args[0]["conversation_id"] == conversation.conversation_id
    assert [m.content for m in inbound_pipeline._claim_pending_messages(conversation)] == ["y una gaseosa"]


@pytest.mark.asyncio
async def test_admin_photos_still_being_read_do_not_reach_the_ai(fake_store):
    """Prueba real 2026-10-19: la foto del admin tarda en leerse más que la
    tanda, y la IA contestaba "el sistema está procesando…" al ver
    "[Imagen recibida]"."""
    business, admin, conversation = _context(fake_store)
    image = _incoming(fake_store, business, admin, conversation, "[Imagen recibida]", 0)
    image.type = "image"

    with patch.object(
        inbound_pipeline, "get_vertical_handlers", return_value=DummyHandlers("admin")
    ), patch.object(inbound_pipeline._agent_runner, "get_reply", new=AsyncMock()) as get_reply:
        await inbound_pipeline.flush_conversation(
            business_id=business.bussiness_id,
            customer_id=admin.customer_id,
            conversation_id=conversation.conversation_id,
        )

    get_reply.assert_not_awaited()
