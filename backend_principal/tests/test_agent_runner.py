"""La sesión ADK guarda business_id/customer_id/conversation_id en su state
(de ahí los leen las tools) y dura un día: así el historial no crece sin
límite ni el modelo reutiliza resultados de tools de días anteriores."""

from datetime import datetime, timezone
import asyncio
from unittest.mock import patch

from google.adk.sessions import InMemorySessionService

from app.ai.ai_restaurant.user.tools.customer_tools import save_order
from app.ai.core import agent_runner
from app.ai.core.agent_runner import AgentRunnerService, _daily_session_id
from app.firestore_db.firebase_schemas import Orders
from app.schemas.enum_schemas import DeliveryStatus
from tests.conftest import seed_active_menu, seed_business, seed_conversation


def _runner() -> AgentRunnerService:
    runner = AgentRunnerService()
    runner._session_service = InMemorySessionService()
    return runner


async def _session(runner, session_id):
    return await runner._session_service.get_session(
        app_name="j2ys-biz-1", user_id="cust-1", session_id=session_id
    )


async def test_human_message_creates_todays_session_with_the_ids_in_state():
    runner = _runner()

    await runner.append_human_message("biz-1", "conv-1", "cust-1", "Ya te atiendo")

    session = await _session(runner, _daily_session_id("conv-1"))
    assert session.state == {
        "business_id": "biz-1",
        "customer_id": "cust-1",
        "conversation_id": "conv-1",
    }
    assert len(session.events) == 1
    assert "Ya te atiendo" in session.events[0].content.parts[0].text


async def test_messages_of_the_same_day_share_the_session():
    runner = _runner()

    await runner.append_human_message("biz-1", "conv-1", "cust-1", "uno")
    await runner.append_human_message("biz-1", "conv-1", "cust-1", "dos")

    session = await _session(runner, _daily_session_id("conv-1"))
    assert len(session.events) == 2


async def test_a_new_day_starts_a_new_session_without_the_old_history():
    runner = _runner()
    day_1 = "conv-1:2026-09-22"
    day_2 = "conv-1:2026-09-30"

    with patch.object(agent_runner, "_daily_session_id", return_value=day_1):
        await runner.append_human_message("biz-1", "conv-1", "cust-1", "menú viejo")
    with patch.object(agent_runner, "_daily_session_id", return_value=day_2):
        await runner.append_human_message("biz-1", "conv-1", "cust-1", "hoy")

    assert len((await _session(runner, day_1)).events) == 1
    today = await _session(runner, day_2)
    assert [e.content.parts[0].text.endswith("hoy") for e in today.events] == [True]


def test_the_day_changes_at_midnight_in_bogota_not_in_utc():
    # 04:30 UTC del 30 = 23:30 del 29 en Bogotá (UTC-5).
    late_night = datetime(2026, 9, 30, 4, 30, tzinfo=timezone.utc)
    next_morning = datetime(2026, 9, 30, 5, 30, tzinfo=timezone.utc)

    assert _daily_session_id("conv-1", late_night) == "conv-1:2026-09-29"
    assert _daily_session_id("conv-1", next_morning) == "conv-1:2026-09-30"


class _ToolContext:
    def __init__(self, state, session_id):
        self.state = state
        self.session = type("S", (), {"id": session_id})()


def test_save_order_keeps_the_real_conversation_id(fake_store):
    """El id de la sesión ya no es el de la conversación: el pedido debe
    guardar el que viaja en el state."""
    business = seed_business(fake_store)
    conversation = seed_conversation(
        fake_store, business, "cust-1", delivery_fee=3000, delivery_status=DeliveryStatus.ZONA.value
    )
    conv_id = conversation.conversation_id
    ctx = _ToolContext(
        {"business_id": business.bussiness_id, "customer_id": "cust-1", "conversation_id": conv_id},
        session_id=f"{conv_id}:2026-09-30",
    )

    seed_active_menu(fake_store, business)
    result = save_order({"items": [{"product": "menu_del_dia", "qty": 1, "elecciones": {"Sopa": "Ajiaco"}}]}, ctx)

    assert result["conversation_id"] == conv_id
    order = next(iter(fake_store.stores[Orders].values()))
    assert order.conversation_id == conv_id


async def test_a_hung_turn_ends_with_the_fallback_reply():
    """Regresión: sin tope, un Vertex colgado retenía el lock de la
    conversación hasta el 504 de Cloud Run y el turno se perdía."""

    class _HungRunner:
        def __init__(self, **kwargs):
            pass

        async def run_async(self, **kwargs):
            await asyncio.sleep(3600)
            yield  # pragma: no cover

    with patch.object(agent_runner, "Runner", _HungRunner), patch.object(
        agent_runner, "AGENT_TURN_TIMEOUT_SECONDS", 0.05
    ):
        reply = await _runner().get_reply(
            agent=object(), business_id="b1", conversation_id="conv-1", user_id="c1", message_text="hola"
        )

    assert reply.text == agent_runner.FALLBACK_REPLY


async def test_system_message_is_not_labeled_as_a_human():
    runner = _runner()

    await runner.append_human_message("biz-1", "conv-1", "cust-1", "¡Recibimos tu pedido!", by_system=True)

    text = (await _session(runner, _daily_session_id("conv-1"))).events[0].content.parts[0].text
    assert "SISTEMA" in text and "HUMANO" not in text
