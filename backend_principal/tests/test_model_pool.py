"""Fallback entre modelos del pool (app/ai/core/model_pool.py): 429/503/timeout
saltan al siguiente modelo sin perder contexto ni repetir tools; 400/401 se
propagan."""

import logging
import time

import httpx
import pytest
from google import genai
from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import errors, types

from app.ai.core import model_pool
from app.ai.core.genai_client import FallbackGemini, with_model_fallback
from app.config import ai_restaurant_settings

POOL = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]


@pytest.fixture(autouse=True)
def _pool(monkeypatch):
    monkeypatch.setattr(ai_restaurant_settings, "MODEL_POOL", ",".join(POOL))
    model_pool._cooldown_until.clear()
    yield
    model_pool._cooldown_until.clear()


def _error(code: int, retry_delay: str | None = None) -> errors.APIError:
    details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}] if retry_delay else []
    body = {"error": {"code": code, "status": "X", "message": "x", "details": details}}
    return (errors.ClientError if code < 500 else errors.ServerError)(code, body)


def _scripted(failing: dict[str, BaseException]):
    """fn(model) que falla para los modelos de `failing` y anota cada intento."""
    tried: list[str] = []

    async def fn(model):
        tried.append(model)
        if model in failing:
            raise failing[model]
        return f"ok:{model}"

    return fn, tried


async def test_429_on_the_primary_goes_to_the_next_model():
    fn, tried = _scripted({POOL[0]: _error(429)})
    assert await model_pool.acall(fn, POOL[0]) == f"ok:{POOL[1]}"
    assert tried == POOL[:2]


async def test_several_429_in_a_row_reach_the_first_healthy_model():
    fn, tried = _scripted({m: _error(429) for m in POOL[:3]})
    assert await model_pool.acall(fn, POOL[0]) == f"ok:{POOL[3]}"
    assert tried == POOL[:4]


@pytest.mark.parametrize("exc", [_error(503), _error(500), _error(404), httpx.ReadTimeout("t"), TimeoutError()])
async def test_503_404_and_timeouts_also_fall_back(exc):
    fn, tried = _scripted({POOL[0]: exc})
    assert await model_pool.acall(fn, POOL[0]) == f"ok:{POOL[1]}"


@pytest.mark.parametrize("code", [400, 401, 403])
async def test_non_recoverable_errors_propagate_without_fallback(code):
    fn, tried = _scripted({POOL[0]: _error(code)})
    with pytest.raises(errors.ClientError):
        await model_pool.acall(fn, POOL[0])
    assert tried == [POOL[0]]
    assert not model_pool._cooldown_until


async def test_a_model_in_cooldown_is_skipped_by_the_next_request():
    fn, _ = _scripted({POOL[0]: _error(429, "7s")})
    await model_pool.acall(fn, POOL[0])
    assert 6 < model_pool._cooldown_until[POOL[0]] - time.monotonic() <= 7

    fn, tried = _scripted({})
    assert await model_pool.acall(fn, POOL[0]) == f"ok:{POOL[1]}"
    assert tried == [POOL[1]]


async def test_when_all_five_fail_it_backs_off_and_retries_the_first_to_cool_down():
    # Enfriamiento de 0 s: en la ronda de backoff el primario ya está libre.
    calls = {"n": 0}

    async def fn(model):
        calls["n"] += 1
        if calls["n"] <= len(POOL):
            raise _error(429, "0s")
        return f"ok:{model}"

    assert await model_pool.acall(fn, POOL[0]) == f"ok:{POOL[0]}"
    assert calls["n"] == len(POOL) + 1


async def test_all_five_exhausted_raises_within_the_latency_budget(caplog):
    fn, tried = _scripted({m: _error(429) for m in POOL})
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=model_pool.__name__), pytest.raises(errors.ClientError):
        await model_pool.acall(fn, POOL[0])
    added = time.monotonic() - started

    # Peor caso medido: los 5 de una vez + reintentos con backoff, sin pasarse.
    print(f"\nlatencia añadida en el peor caso: {added:.2f}s en {len(tried)} intentos")
    assert tried[:5] == POOL and len(tried) > 5
    assert added <= model_pool.BACKOFF_BUDGET_SECONDS + 0.2
    fields = caplog.records[-1].fields
    assert fields["model_fallback"] == "exhausted" and fields["served_by"] is None
    assert {f["model"] for f in fields["failed_models"]} == set(POOL)


async def test_each_fallback_is_logged_without_message_content(caplog):
    fn, _ = _scripted({POOL[0]: _error(429)})
    with caplog.at_level(logging.WARNING, logger=model_pool.__name__):
        await model_pool.acall(fn, POOL[0])
    (record,) = caplog.records
    assert record.fields["failed_models"] == [{"model": POOL[0], "code": 429}]
    assert record.fields["served_by"] == POOL[1]
    assert set(record.fields) == {"model_fallback", "failed_models", "served_by", "added_ms"}


def test_sync_client_falls_back_too():
    tried = []

    def fn(model):
        tried.append(model)
        if model == POOL[0]:
            raise _error(429)
        return model

    assert model_pool.call(fn, POOL[0]) == POOL[1]


def test_pool_rejects_preview_and_2x_models():
    with pytest.raises(ValueError):
        type(ai_restaurant_settings)(MODEL_POOL="gemini-3.8-flash,gemini-3-flash-preview")
    with pytest.raises(ValueError):
        type(ai_restaurant_settings)(MODEL_POOL="gemini-2.5-flash")


# --- Integración con ADK: 429 a mitad de una cadena de tool calls ----------


def _response(model: str, part: types.Part) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[part]), finish_reason="STOP")],
        model_version=model,
    )


async def test_429_mid_tool_chain_keeps_session_and_context_and_runs_tools_once():
    executed = []

    def crear_reserva(nombre: str) -> dict:
        """Crea una reserva (efecto secundario)."""
        executed.append(nombre)
        return {"reserva": "R-1"}

    requests = []  # (modelo, contents, config) de cada intento

    async def raw_generate(*, model, contents, config):
        requests.append((model, [c.model_dump() for c in contents], config.model_dump()))
        if len(requests) == 1:
            return _response(model, types.Part.from_function_call(name="crear_reserva", args={"nombre": "Ana"}))
        if model == POOL[0]:  # el 2.º paso (ya con el resultado de la tool) da 429
            raise _error(429)
        return _response(model, types.Part.from_text(text="Listo, reserva R-1"))

    client = genai.Client(vertexai=False, api_key="fake")
    client.aio.models.generate_content = raw_generate
    agent = Agent(
        name="t",
        model=FallbackGemini(model=POOL[0], client=with_model_fallback(client)),
        instruction="Eres el asistente del restaurante.",
        tools=[crear_reserva],
        generate_content_config=types.GenerateContentConfig(temperature=0.3),
    )
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="t", user_id="u", session_id="s-1")
    runner = Runner(agent=agent, app_name="t", session_service=sessions)

    events = [
        e
        async for e in runner.run_async(
            user_id="u", session_id=session.id, new_message=types.Content(role="user", parts=[types.Part.from_text(text="reserva para Ana")])
        )
    ]

    assert executed == ["Ana"]  # la tool corrió una sola vez
    assert [m for m, _, _ in requests] == [POOL[0], POOL[0], POOL[1]]
    # El modelo de relevo recibió exactamente el mismo contexto y config.
    (_, contents_failed, config_failed), (_, contents_retry, config_retry) = requests[1], requests[2]
    assert contents_retry == contents_failed
    assert config_retry == config_failed
    assert config_retry["temperature"] == 0.3 and "restaurante" in str(config_retry["system_instruction"])
    assert any(p.get("function_response") for c in contents_retry for p in c["parts"])

    stored = await sessions.get_session(app_name="t", user_id="u", session_id="s-1")
    assert len(stored.events) == len(events) + 1 == 4  # user, call, response, texto final
    assert sum(1 for e in stored.events if e.get_function_responses()) == 1
    assert events[-1].content.parts[0].text == "Listo, reserva R-1"
    assert events[-1].model_version == POOL[1]


async def test_chained_call_keeps_the_client_alive_during_the_request():
    """Regresión (Vertex real 2026-10-20): con build_genai_client().aio.models...
    encadenado, el Client se liberaba a mitad de la llamada y cerraba la
    conexión ("Cannot send a request, as the client has been closed")."""
    import gc
    import weakref

    refs = []

    async def raw(*, model, **kw):
        gc.collect()
        assert refs[0]() is not None, "el cliente se liberó a mitad de la llamada"
        return "ok"

    def build():
        client = genai.Client(vertexai=False, api_key="fake")
        client.aio.models.generate_content = raw
        refs.append(weakref.ref(client))
        return with_model_fallback(client)

    assert await build().aio.models.generate_content(model=POOL[0], contents="x") == "ok"


async def test_a_hung_model_is_cut_by_the_hard_timeout_and_the_next_one_answers():
    """Vertex real dejó colgado un modelo 56 s pese al timeout de httpx."""
    import asyncio

    async def raw(*, model, **kw):
        if model == POOL[0]:
            await asyncio.sleep(5)
        return f"ok:{model}"

    client = genai.Client(vertexai=False, api_key="fake")
    client.aio.models.generate_content = raw
    started = time.monotonic()
    result = await with_model_fallback(client, timeout_ms=200).aio.models.generate_content(model=POOL[0], contents="x")
    assert result == f"ok:{POOL[1]}"
    assert time.monotonic() - started < 1


async def test_a_504_keeps_the_model_out_for_two_minutes():
    fn, _ = _scripted({POOL[0]: _error(504)})
    await model_pool.acall(fn, POOL[0])
    assert 119 < model_pool._cooldown_until[POOL[0]] - time.monotonic() <= 120

def test_the_requested_model_goes_first_and_the_rest_keep_their_order():
    # El agente de clientes en flash-lite: si falla, sigue el 3.8 y los demás.
    assert model_pool.pool("gemini-3.5-flash-lite") == ["gemini-3.5-flash-lite", *POOL[:-1]]
    assert model_pool.pool(POOL[0]) == POOL
