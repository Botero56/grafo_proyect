"""F2: la sesión ADK no crece sin límite. Una escritura por turno, sin los
bytes de audio, y la historia vieja se reemplaza por un resumen que viaja en
la instrucción del agente (prueba real 2026-10-05: 810 KB, 179 eventos y
11 audios en una sesión de un día; el tope de Firestore es 1 MiB)."""

import json
import logging
from unittest.mock import AsyncMock, patch

from google.adk.events.event import Event
from google.adk.sessions import InMemorySessionService
from google.adk.sessions.session import Session
from google.genai import types

from app.ai.core import agent_runner, conversation_memory
from app.ai.core.agent_runner import AgentRunnerService
from app.ai.core.firestore_session_service import FirestoreSessionService


# --- eventos de prueba ------------------------------------------------------


def _user(text="hola", audio=False):
    parts = [types.Part.from_text(text=text)]
    if audio:
        parts.append(types.Part.from_bytes(data=b"\x00" * 50_000, mime_type="audio/ogg"))
    return Event(author="user", content=types.Content(role="user", parts=parts))


def _call(name="get_active_menu"):
    return Event(author="customer_agent", content=types.Content(
        role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args={}))]))


def _result(name="get_active_menu"):
    return Event(author="customer_agent", content=types.Content(
        role="user", parts=[types.Part(function_response=types.FunctionResponse(name=name, response={"ok": True}))]))


def _answer(text="Listo"):
    return Event(author="customer_agent", content=types.Content(role="model", parts=[types.Part.from_text(text=text)]))


def _turn(i):
    return [_user(f"mensaje {i}"), _call(), _result(), _answer(f"respuesta {i}")]


# --- servicio de sesiones con un Firestore falso ----------------------------


class _Doc:
    def __init__(self, store, key):
        self.store, self.key = store, key

    async def set(self, payload):
        self.store["writes"].append(self.key)
        self.store[self.key] = payload


def _service():
    svc = FirestoreSessionService.__new__(FirestoreSessionService)
    svc._dirty, svc._write_stats = {}, {}
    store = {"writes": []}
    svc._doc_ref = lambda app, user, sid: _Doc(store, sid)
    return svc, store


def _session():
    return Session(app_name="a", user_id="u", id="s1", state={}, events=[])


async def test_a_turn_with_tools_is_written_once():
    svc, store = _service()
    session = _session()

    for event in [_user(), _call(), _result(), _answer()]:
        await svc.append_event(session, event)
    await svc.flush_session("a", "u", "s1")

    assert store["writes"] == ["s1"]  # antes: una escritura completa por evento
    assert svc.pop_write_stats("s1")["writes"] == 1


async def test_a_turn_that_failed_after_a_tool_is_still_saved():
    """Si el turno muere después de save_order, la historia debe mostrar que
    la tool corrió: si no, la IA podría volver a guardar el pedido."""
    svc, store = _service()
    session = _session()
    for event in [_user(), _call("save_order"), _result("save_order")]:
        await svc.append_event(session, event)
    assert store["writes"] == []

    await svc.flush_session("a", "u", "s1")

    saved = json.loads(store["s1"]["data"])
    assert [e["author"] for e in saved["events"]] == ["user", "customer_agent", "customer_agent"]


async def test_audio_bytes_are_not_stored():
    svc, store = _service()
    session = _session()

    await svc.append_event(session, _user("[Audio recibido]", audio=True))
    await svc.append_event(session, _answer())

    data = store["s1"]["data"]
    assert len(data) < 5_000  # 50 KB de audio fuera
    assert "[audio del cliente, ya respondido]" in data
    # El turno en curso sí tuvo el audio (vive en memoria, no en Firestore).
    assert session.events[0].content.parts[1].inline_data is not None


# --- corte y resumen ---------------------------------------------------------


def test_no_compaction_below_the_limit():
    events = [e for i in range(conversation_memory.MAX_EVENTS // 4) for e in _turn(i)]
    assert conversation_memory.split_point(events) is None


def test_the_cut_starts_at_a_customer_turn_and_keeps_the_recent_ones():
    events = [e for i in range(20) for e in _turn(i)]  # 80 eventos

    cut = conversation_memory.split_point(events)

    assert events[cut].author == "user"  # no separa una tool de su respuesta
    assert len(events) - cut >= conversation_memory.KEEP_EVENTS


def test_the_summary_travels_in_the_instruction():
    instruction = conversation_memory.with_conversation_summary("PROMPT {sin_variables}")

    class _Ctx:
        def __init__(self, state):
            self.state = state

    assert instruction(_Ctx({})) == "PROMPT {sin_variables}"
    with_summary = instruction(_Ctx({conversation_memory.SUMMARY_KEY: "Ana pidió 2 menús, order_id X."}))
    assert with_summary.startswith("PROMPT {sin_variables}") and "Ana pidió 2 menús" in with_summary


# --- compactación desde el runner --------------------------------------------


class _CompactableService(InMemorySessionService):
    def __init__(self):
        super().__init__()
        self.replaced = None

    async def replace_history(self, session, events, state_updates):
        self.replaced = (list(events), dict(state_updates))
        session.events = list(events)
        session.state.update(state_updates)


async def test_the_runner_replaces_old_history_with_a_summary():
    runner = AgentRunnerService()
    runner._session_service = _CompactableService()
    session = _session()
    session.events = [e for i in range(20) for e in _turn(i)]

    with patch.object(conversation_memory, "summarize", new=AsyncMock(return_value="Resumen de Ana")) as summarize:
        await runner._compact_if_needed(session)

    kept, state = runner._session_service.replaced
    assert state == {conversation_memory.SUMMARY_KEY: "Resumen de Ana"}
    assert kept[0].author == "user" and len(kept) < 80
    old_events = summarize.await_args.args[1]
    assert len(old_events) + len(kept) == 80


async def test_if_the_summary_fails_the_session_stays_whole():
    runner = AgentRunnerService()
    runner._session_service = _CompactableService()
    session = _session()
    session.events = [e for i in range(20) for e in _turn(i)]

    with patch.object(conversation_memory, "summarize", new=AsyncMock(side_effect=RuntimeError("429"))):
        result = await runner._compact_if_needed(session)

    assert runner._session_service.replaced is None
    assert len(result.events) == 80


# --- F1: una línea de métricas por turno -------------------------------------


async def test_each_turn_logs_its_metrics(caplog):
    class _Runner:
        def __init__(self, **kwargs):
            pass

        async def run_async(self, **kwargs):
            event = _answer("Hola, ¿qué deseas?")
            event.usage_metadata = types.GenerateContentResponseUsageMetadata(
                prompt_token_count=8000, candidates_token_count=40, thoughts_token_count=0
            )
            yield event

    runner = AgentRunnerService()
    runner._session_service = InMemorySessionService()
    with patch.object(agent_runner, "Runner", _Runner), caplog.at_level(logging.INFO):
        reply = await runner.get_reply(
            agent=type("A", (), {"name": "customer_agent"})(), business_id="b1",
            conversation_id="conv-1", user_id="c1", message_text="hola",
        )

    assert reply.text == "Hola, ¿qué deseas?"
    record = next(r for r in caplog.records if r.getMessage() == "Turno de IA")
    assert record.fields["outcome"] == "ok"
    assert (record.fields["llm_calls"], record.fields["prompt_tokens"], record.fields["output_tokens"]) == (1, 8000, 40)
    assert record.fields["agent"] == "customer_agent" and record.fields["seconds"] >= 0

def test_the_state_goes_last_so_the_cached_prefix_does_not_change():
    from google.genai import types

    class _Ctx:
        state = {"pedido": "2 menus"}

    class _Req:
        def __init__(self):
            self.contents = [types.Content(role="user", parts=[types.Part(text="hola")])]

    callback = conversation_memory.append_current_state(lambda state: f"Pedido: {state['pedido']}")
    request = _Req()
    assert callback(_Ctx(), request) is None
    assert request.contents[0].parts[0].text == "hola"  # lo anterior no cambia
    assert request.contents[-1].parts[0].text == conversation_memory.STATE_HEADER + "Pedido: 2 menus"

    empty = _Req()
    conversation_memory.append_current_state(lambda state: "  ")(_Ctx(), empty)
    assert len(empty.contents) == 1
