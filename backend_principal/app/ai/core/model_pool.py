"""Fallback entre modelos Gemini (MODEL_POOL) ante 429, 5xx o timeout.

Se engancha a nivel de UNA llamada al modelo (generate_content), no del
turno: ADK solo ejecuta tools después de una respuesta exitosa y la sesión la
escribe el Runner, así que reintentar la misma petición con otro modelo lleva
el mismo historial, resultados de tools, system prompt, tools y config, y
ninguna tool corre dos veces.

- Un modelo que falla queda en enfriamiento (retryDelay de la respuesta si
  viene, si no unos segundos) y las siguientes peticiones lo saltan.
- Si todos fallan: backoff exponencial con jitter hasta BACKOFF_BUDGET_SECONDS
  y luego se propaga el último error (el agent_runner responde FALLBACK_REPLY).
- 400/401/403 y demás se propagan sin fallback.
"""

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable, Optional

import httpx
from google.genai import errors

from app.config import ai_restaurant_settings

logger = logging.getLogger(__name__)

# 404: el modelo no existe en la región o ya se retiró -> el siguiente sí.
RECOVERABLE_CODES = {404, 429, 500, 502, 503, 504}
COOLDOWN_429_SECONDS = 30.0
# Timeouts y 5xx: con 10 s, un modelo que cuelga 25 s por llamada se volvía a
# probar en la siguiente llamada del mismo turno (real 2026-10-21: turno de
# 76 s, dos 504 de gemini-3.8-flash). Saltarlo 2 min evita pagar el cuelgue.
COOLDOWN_OTHER_SECONDS = 120.0
MAX_COOLDOWN_SECONDS = 120.0
# Lo máximo que el cliente espera de más cuando TODO el pool está fallando.
BACKOFF_BUDGET_SECONDS = 2.5

# Por instancia de Cloud Run: modelo -> time.monotonic() hasta el que se salta.
_cooldown_until: dict[str, float] = {}


def pool(model: str) -> list[str]:
    """El modelo pedido primero y después el resto de MODEL_POOL en su orden;
    sin pool, solo el modelo pedido."""
    rest = [m for m in ai_restaurant_settings.model_pool if m != model]
    return [model, *rest] if ai_restaurant_settings.model_pool else [model]


def _error_code(exc: BaseException) -> Optional[int | str]:
    if isinstance(exc, errors.APIError):
        return exc.code if exc.code in RECOVERABLE_CODES else None
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    return None


def _retry_delay(exc: BaseException) -> Optional[float]:
    """RetryInfo.retryDelay ("7s") que Vertex manda en algunos 429."""
    try:
        for detail in exc.details["error"]["details"]:
            if "retryDelay" in detail:
                return float(str(detail["retryDelay"]).rstrip("s"))
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    return None


class _Plan:
    def __init__(self, model: str) -> None:
        self.models = pool(model)
        self.started = time.monotonic()
        self.failures: list[dict] = []
        self.round = 0

    def candidates(self) -> list[str]:
        now = time.monotonic()
        ready = [m for m in self.models if _cooldown_until.get(m, 0) <= now]
        # Todos enfriándose: se intenta el que se libera antes.
        return ready or [min(self.models, key=lambda m: _cooldown_until.get(m, 0))]

    def failed(self, model: str, exc: BaseException) -> bool:
        """Registra el fallo; False si no es recuperable (se propaga)."""
        code = _error_code(exc)
        if code is None:
            return False
        retry_delay = _retry_delay(exc)
        if retry_delay is None:
            retry_delay = COOLDOWN_429_SECONDS if code == 429 else COOLDOWN_OTHER_SECONDS
        cooldown = min(retry_delay, MAX_COOLDOWN_SECONDS)
        _cooldown_until[model] = time.monotonic() + cooldown
        self.failures.append({"model": model, "code": code})
        return True

    def backoff(self) -> Optional[float]:
        """Espera antes de la siguiente ronda, o None si se acabó el presupuesto."""
        self.round += 1
        delay = min(0.25 * 2**self.round, 1.0) * random.uniform(0.5, 1.0)
        if time.monotonic() - self.started + delay > BACKOFF_BUDGET_SECONDS:
            return None
        return delay

    def log(self, served_by: Optional[str]) -> None:
        if not self.failures:
            return
        # Sin contenido del mensaje: solo modelos, códigos y tiempos. Los ids
        # de negocio/conversación/sesión los agrega logging_setup.bind().
        logger.warning(
            "Fallback de modelo",
            extra={
                "fields": {
                    "model_fallback": "recovered" if served_by else "exhausted",
                    "failed_models": self.failures,
                    "served_by": served_by,
                    "added_ms": round((time.monotonic() - self.started) * 1000),
                }
            },
        )


async def acall(fn: Callable[[str], Awaitable[Any]], model: str) -> Any:
    plan = _Plan(model)
    while True:
        for candidate in plan.candidates():
            try:
                result = await fn(candidate)
            except Exception as exc:
                if not plan.failed(candidate, exc):
                    raise
                last = exc
                continue
            plan.log(candidate)
            return result
        delay = plan.backoff()
        if delay is None:
            plan.log(None)
            raise last
        await asyncio.sleep(delay)


def call(fn: Callable[[str], Any], model: str) -> Any:
    """Igual que acall, para los extractores que usan el cliente síncrono."""
    plan = _Plan(model)
    while True:
        for candidate in plan.candidates():
            try:
                result = fn(candidate)
            except Exception as exc:
                if not plan.failed(candidate, exc):
                    raise
                last = exc
                continue
            plan.log(candidate)
            return result
        delay = plan.backoff()
        if delay is None:
            plan.log(None)
            raise last
        time.sleep(delay)
