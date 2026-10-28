"""Cliente de google.genai para todas las llamadas al modelo: los agentes ADK
(agent_model) y las directas (menú del día, corrección, comprobante, audio,
resumen y reply_guard). Usa la misma configuración de Vertex AI que google-adk
(ver app/config.py: VertexAISettings). Toda llamada pasa por el fallback del
pool de modelos (app/ai/core/model_pool.py)."""

import asyncio
from functools import cached_property

from google import genai
from google.adk.models.google_llm import Gemini
from google.genai import types

from app.ai.core import model_pool
from app.config import ai_restaurant_settings, vertex_ai_settings

# Sin timeout, una llamada colgada a Vertex retenía la request hasta que
# Cloud Run la cortaba (504) y el turno se perdía. 90 s cubre de sobra una
# imagen de menú o de comprobante.
_REQUEST_TIMEOUT_MS = 90_000
# Producción (14 días a 2026-10-20): una llamada de agente p50 3 s, p90 11 s,
# con cuelgues de hasta 119 s. Contra Vertex real, gemini-3.8-flash tardó 56 s
# en dar 504 PESE al timeout de httpx (no limita la duración total), por eso
# las llamadas async además llevan un tope duro con asyncio.wait_for.
_AGENT_TIMEOUT_MS = 25_000

# Sin retry_options genai NO reintenta, que es lo que queremos: ante un 429
# se salta al siguiente modelo del pool en vez de esperar al mismo.


def with_model_fallback(client: genai.Client, timeout_ms: int | None = None) -> genai.Client:
    """Hace que generate_content (sync y async) recorra MODEL_POOL. En async,
    `timeout_ms` es el tope duro por modelo antes de pasar al siguiente."""
    hard_timeout = timeout_ms / 1000 if timeout_ms else None
    models, aio_models = client.models, client.aio.models
    sync_call, async_call = models.generate_content, aio_models.generate_content
    # Las funciones referencian `client` a propósito: con la llamada encadenada
    # (build_genai_client().aio.models...) el Client se liberaba a mitad de
    # request y cerraba su conexión (probado contra Vertex real).
    def generate(*, model, **kw):
        return model_pool.call(lambda m: client and sync_call(model=m, **kw), model)

    async def agenerate(*, model, **kw):
        return await model_pool.acall(
            lambda m: client and asyncio.wait_for(async_call(model=m, **kw), hard_timeout), model
        )

    models.generate_content = generate
    aio_models.generate_content = agenerate
    # shortcut: generate_content_stream no tiene fallback; no usamos streaming. Si
    # se activa, reintentar solo si aún no se emitió ningún chunk.
    return client


def build_genai_client(timeout_ms: int = _REQUEST_TIMEOUT_MS) -> genai.Client:
    return with_model_fallback(
        genai.Client(
            vertexai=vertex_ai_settings.GOOGLE_GENAI_USE_VERTEXAI,
            project=vertex_ai_settings.GOOGLE_CLOUD_PROJECT,
            location=vertex_ai_settings.GOOGLE_CLOUD_LOCATION,
            http_options=types.HttpOptions(timeout=timeout_ms),
        ),
        timeout_ms,
    )


class FallbackGemini(Gemini):
    """Gemini de ADK con el cliente del pool (creado al primer uso, como ADK)."""

    @cached_property
    def api_client(self) -> genai.Client:
        return self.client or build_genai_client(_AGENT_TIMEOUT_MS)


def agent_model(model: str | None = None) -> Gemini:
    """El modelo de un agente ADK (por defecto MODEL_RESTAURANT), con fallback
    al resto del pool."""
    return FallbackGemini(model=model_pool.pool(model or ai_restaurant_settings.MODEL_RESTAURANT)[0])
