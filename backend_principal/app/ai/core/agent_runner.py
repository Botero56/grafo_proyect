"""
Ejecución de agentes ADK: convierte un Agent ya construido + un mensaje de
texto en una respuesta de texto. Es genérico entre verticales: recibe el
Agent ya resuelto (por app.ai.vertical_registry), no sabe de restaurantes
ni de odontología.
"""

import asyncio
import copy
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Callable, Optional

from google.adk.agents import Agent
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.runners import Runner
from google.adk.sessions.session import Session
from google.genai import types

from app.ai.core import conversation_memory
from app.ai.core.firestore_session_service import FirestoreSessionService
from app.ai.core.reply_guard import guard_reply, to_whatsapp_format
from app.logging_setup import bind
from app.services.excel_report_service.constants import DEFAULT_TZ

logger = logging.getLogger(__name__)

# Cubre varias llamadas al modelo con tools; queda bajo el timeout
# de 300 s de Cloud Run para que el turno termine con una respuesta.
AGENT_TURN_TIMEOUT_SECONDS = 150

FALLBACK_REPLY = "Tuvimos un problema respondiendo tu mensaje, intenta de nuevo en un momento."


@dataclass
class AgentReply:
    text: str
    # Si una tool (ej. generate_excel_report) dejó un archivo listo para
    # enviar, viaja acá como {"path": "...", "filename": "..."}; el
    # webhook lo envía por WhatsApp después del texto. None si no aplica.
    pending_document: Optional[dict] = None
    # Seteados cuando el agente llamó a request_human_handoff en este turno
    # (ver app/ai/ai_restaurant/user/tools/customer_tools.py). Si
    # requires_human es True, el webhook debe ignorar `text` y mandar el
    # mensaje de traspaso configurado en su lugar (ver
    # app/services/conversation/handoff_service.py).
    requires_human: bool = False
    handoff_reason: Optional[str] = None
    handoff_summary: Optional[str] = None
    handoff_topic: Optional[str] = None


def _app_name(business_id: str) -> str:
    return f"j2ys-{business_id}"


def _daily_session_id(conversation_id: str, now: Optional[datetime] = None) -> str:
    """Una sesión ADK por conversación y por día del negocio.

    Antes la sesión era la conversación entera, que nunca se cierra: el
    historial crecía sin límite (461 KB en 8 días, el tope de un documento
    Firestore es 1 MiB) y el modelo respondía con resultados de tools de
    días anteriores -- un menú del 22/09 con precios ya corregidos, en la
    prueba real del 2026-09-30. Cada día empieza sin ese historial; lo que
    debe persistir (perfil, pedidos, menú) vive en Firestore y se lee con
    las tools."""
    day = (now or datetime.now(DEFAULT_TZ)).astimezone(DEFAULT_TZ).date().isoformat()
    return f"{conversation_id}:{day}"


class AgentRunnerService:
    """El historial de conversación (turnos, tool calls) se persiste en
    Firestore vía FirestoreSessionService (ver ese módulo), así que sobrevive
    un restart del proceso -- antes se perdía por completo porque usaba
    InMemorySessionService de ADK. Los locks de `_locks` sí siguen siendo
    solo en memoria/por proceso (no hace falta que sobrevivan un restart,
    solo evitan que dos mensajes de la misma conversación se procesen
    solapados dentro del mismo proceso vivo)."""

    def __init__(self) -> None:
        self._session_service = FirestoreSessionService()
        # InMemorySessionService NO soporta dos runs concurrentes sobre el
        # mismo conversation_id: si dos mensajes del mismo chat se procesan
        # solapados (dos mensajes seguidos, o uno que WhatsApp entrega
        # tarde mientras el anterior sigue corriendo), el historial de la
        # sesión se mezcla entre ambos turnos y el modelo termina
        # respondiendo/actuando sobre los dos mensajes a la vez. Un
        # asyncio.Lock por conversation_id serializa esos turnos: el
        # segundo espera a que el primero termine antes de arrancar, igual
        # que una conversación real. Se acumula un lock por conversation_id
        # vista durante la vida del proceso (mismo trade-off ya documentado
        # arriba para _session_service).
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _ensure_session(
        self, business_id: str, conversation_id: str, user_id: str, session_scope: Optional[str] = None
    ) -> Session:
        """La sesión ADK de HOY para la conversación (ver _daily_session_id),
        creándola la primera vez.

        business_id/customer_id/conversation_id ya los resolvió el webhook
        antes de llegar acá: se guardan en el state de la sesión para que las
        tools los lean vía ToolContext, en vez de que el modelo tenga que
        pedírselos al usuario o inventarlos."""
        app_name = _app_name(business_id)
        session_id = _daily_session_id(conversation_id)
        if session_scope:
            # Otro agente en el mismo chat (ej. el domiciliario que hoy pide
            # como cliente): historial aparte, que no se mezclen.
            session_id = f"{session_id}:{session_scope}"
        session = await self._session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        if session is None:
            session = await self._session_service.create_session(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
                state={
                    "business_id": business_id,
                    "customer_id": user_id,
                    "conversation_id": conversation_id,
                },
            )
        return session

    async def _compact_if_needed(self, session: Session) -> Session:
        """Si la sesión pasó de conversation_memory.MAX_EVENTS, resume lo
        viejo y deja solo los últimos turnos (ver ese módulo). Best-effort:
        si el resumen falla, la sesión sigue completa y se reintenta en el
        próximo turno."""
        cut = conversation_memory.split_point(session.events)
        replace_history = getattr(self._session_service, "replace_history", None)
        if cut is None or replace_history is None:
            return session
        try:
            summary = await conversation_memory.summarize(
                session.state.get(conversation_memory.SUMMARY_KEY), session.events[:cut]
            )
            await replace_history(session, session.events[cut:], {conversation_memory.SUMMARY_KEY: summary})
            logger.info(
                "Sesión compactada con resumen",
                extra={"fields": {"eventos_resumidos": cut, "eventos_conservados": len(session.events)}},
            )
        except Exception:
            logger.exception("No se pudo resumir la sesión; sigue completa")
        return session

    async def get_reply(
        self,
        agent: Agent,
        business_id: str,
        conversation_id: str,
        user_id: str,
        message_text: str,
        session_scope: Optional[str] = None,
    ) -> AgentReply:
        async with self._locks[conversation_id]:
            return await self._get_reply_locked(
                agent=agent,
                business_id=business_id,
                conversation_id=conversation_id,
                user_id=user_id,
                message_text=message_text,
                session_scope=session_scope,
            )

    async def _get_reply_locked(
        self,
        agent: Agent,
        business_id: str,
        conversation_id: str,
        user_id: str,
        message_text: str,
        session_scope: Optional[str] = None,
    ) -> AgentReply:
        started = time.monotonic()
        usage = {"llm_calls": 0, "prompt_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "thought_tokens": 0, "tools": [], "models": []}
        outcome = "ok"
        session = None
        try:
            session = await self._ensure_session(business_id, conversation_id, user_id, session_scope)
            session = await self._compact_if_needed(session)
            bind(session_id=session.id)
            order_before = repr(session.state.get("pedido"))

            runner = Runner(
                agent=agent,
                app_name=_app_name(business_id),
                session_service=self._session_service,
            )

            # Las notas de voz ya llegan transcritas (ver inbound_pipeline).
            new_message = types.Content(role="user", parts=[types.Part.from_text(text=message_text)])

            # pending_document se captura del state_delta de LOS EVENTOS DE
            # ESTA llamada a run_async, no releyendo Session.state después
            # (que es compartido por toda la conversación). Dos mensajes
            # seguidos del mismo usuario pueden procesarse solapados (el
            # propio WhatsApp puede entregar uno tarde y que llegue mientras
            # el anterior sigue corriendo); si se lee desde el state
            # compartido, un turno puede robarse o pisar el pending_document
            # que dejó otro turno concurrente. Leyéndolo del stream de
            # eventos de esta invocación puntual, cada turno solo ve lo que
            # sus propias tools escribieron.
            reply_parts: list[str] = []
            pending_document = None
            requires_human = False
            handoff_reason = None
            handoff_summary = None
            handoff_topic = None
            events = runner.run_async(
                user_id=user_id, session_id=session.id, new_message=new_message
            )
            # Tope al turno completo (todas las llamadas al modelo y tools):
            # sin esto un Vertex colgado retenía el lock de la conversación
            # hasta que Cloud Run cortaba la request con 504. Al vencer cae al
            # except de abajo y se responde FALLBACK_REPLY.
            async with asyncio.timeout(AGENT_TURN_TIMEOUT_SECONDS):
                async for event in events:
                    if event.usage_metadata:
                        um = event.usage_metadata
                        usage["llm_calls"] += 1
                        usage["prompt_tokens"] += um.prompt_token_count or 0
                        # Parte de prompt_tokens servida desde la caché implícita (~10x más barata).
                        usage["cached_tokens"] += um.cached_content_token_count or 0
                        usage["output_tokens"] += um.candidates_token_count or 0
                        usage["thought_tokens"] += um.thoughts_token_count or 0
                    usage["tools"] += [call.name for call in event.get_function_calls()]
                    # Qué modelo del pool respondió cada paso (ver model_pool).
                    if event.model_version and event.model_version not in usage["models"]:
                        usage["models"].append(event.model_version)
                    if event.content and event.content.parts:
                        # `not part.thought`: en modelos con razonamiento (ej.
                        # gemini-3.8-flash) el stream de eventos incluye "thought
                        # parts" -- el razonamiento interno del modelo, expuesto
                        # también como .text -- separados de la respuesta final.
                        # Sin este filtro, cuando el modelo divaga (típicamente
                        # al alucinar) ese razonamiento se cuela concatenado en
                        # la respuesta que le llega al cliente/admin.
                        reply_parts.extend(
                            part.text
                            for part in event.content.parts
                            if part.text and not part.thought
                        )
                    if event.actions and event.actions.state_delta:
                        delta = event.actions.state_delta
                        doc = delta.get("pending_document")
                        if doc is not None:
                            pending_document = doc
                        if delta.get("requiere_humano"):
                            requires_human = True
                            handoff_reason = delta.get("motivo_handoff")
                            handoff_summary = delta.get("resumen_handoff")
                            handoff_topic = delta.get("tema_handoff")

            reply_text = "".join(reply_parts).strip()
            if not reply_text:
                # El cliente recibe el mensaje de respaldo; sin este aviso no
                # quedaba rastro de por qué (prueba real 2026-10-05, un audio).
                logger.warning("El modelo terminó el turno sin texto; se responde FALLBACK_REPLY")
                reply_text = FALLBACK_REPLY
                outcome = "sin_texto"
            # Red de seguridad adicional (ver app/ai/core/reply_guard.py):
            # cubre el caso en que el texto "final" del modelo -- ya sin
            # thought parts -- igual quedó largo o con razonamiento en
            # primera persona filtrado. No dispara nada si el texto luce
            # normal.
            reply_text = to_whatsapp_format(await guard_reply(reply_text))
            usage["pedido_cambio"] = repr(session.state.get("pedido")) != order_before

            return AgentReply(
                text=reply_text,
                pending_document=pending_document,
                requires_human=requires_human,
                handoff_reason=handoff_reason,
                handoff_summary=handoff_summary,
                handoff_topic=handoff_topic,
            )

        except Exception as exc:
            outcome = (
                "timeout" if isinstance(exc, TimeoutError)
                else "429" if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc)
                else "error"
            )
            logger.exception(
                "Fallo ejecutando el agente (business_id=%s, conversation_id=%s)",
                business_id,
                conversation_id,
            )
            return AgentReply(text=FALLBACK_REPLY)
        finally:
            await self._finish_turn(agent, session, started, usage, outcome)

    async def _finish_turn(self, agent, session: Optional[Session], started: float, usage: dict, outcome: str) -> None:
        """Escribe lo pendiente de la sesión y deja UNA línea de métricas por
        turno (F1): llamadas al modelo, tokens, latencia, resultado y tamaño
        de la sesión. Filtrable en Cloud Logging por jsonPayload.<campo>."""
        stats = {"writes": 0, "bytes": 0, "events": None}
        if session is not None:
            try:
                flush = getattr(self._session_service, "flush_session", None)
                if flush is not None:
                    await flush(session.app_name, session.user_id, session.id)
                pop = getattr(self._session_service, "pop_write_stats", None)
                if pop is not None:
                    stats = pop(session.id)
            except Exception:
                logger.exception("No se pudo guardar la sesión al terminar el turno")
        logger.info(
            "Turno de IA",
            extra={
                "fields": {
                    "agent": getattr(agent, "name", None),
                    "outcome": outcome,
                    "seconds": round(time.monotonic() - started, 2),
                    **usage,
                    "session_events": stats["events"],
                    "session_writes": stats["writes"],
                    "session_kb": round(stats["bytes"] / 1024, 1),
                }
            },
        )

    async def append_human_message(
        self, business_id: str, conversation_id: str, user_id: str, text: str, by_system: bool = False
    ) -> None:
        """Registra en la sesión ADK que un HUMANO (no la IA) le escribió
        directamente al cliente (echo de smb_message_echoes), para que
        cuando el bot se reactive tenga ese turno en su contexto y no lo
        repita ni lo contradiga. Best-effort: si
        falla, se loguea y no se propaga -- nunca debe romper el flujo del
        webhook."""
        try:
            async with self._locks[conversation_id]:
                session = await self._ensure_session(business_id, conversation_id, user_id)

                if by_system:
                    # Avisos automáticos (carrito, cotización, incidencia,
                    # pago): no hay un humano atendiendo, la IA sigue.
                    who = "el SISTEMA le envió este mensaje automático al cliente, sin pasar por ti. No lo repitas ni lo contradigas; sigue atendiendo tú."
                else:
                    who = "un HUMANO del equipo le escribió esto directamente al cliente desde la app de WhatsApp Business, sin pasar por ti. No lo repitas ni lo contradigas si retomas la conversación."
                note = f"[Nota del sistema, no visible para el cliente: {who}]\n{text}"
                content = types.Content(role="model", parts=[types.Part.from_text(text=note)])
                await self._session_service.append_event(
                    session, Event(author="human_agent", content=content)
                )
        except Exception:
            logger.exception(
                "No se pudo registrar el mensaje humano en la sesión ADK "
                "(business_id=%s, conversation_id=%s)",
                business_id,
                conversation_id,
            )

    async def edit_state(self, business_id: str, conversation_id: str, user_id: str, fn: Callable[[dict], tuple]):
        """Cambia el state de la sesión de HOY por fuera de un turno (ej. el
        carrito del catálogo) con el mismo lock que los turnos. `fn` recibe
        una copia del state y devuelve (cambios o None, resultado); los
        cambios quedan como un evento sin texto."""
        async with self._locks[conversation_id]:
            session = await self._ensure_session(business_id, conversation_id, user_id)
            delta, result = fn(copy.deepcopy(session.state))
            if delta:
                await self._session_service.append_event(
                    session, Event(author="system", actions=EventActions(state_delta=delta))
                )
            return result


@lru_cache(maxsize=1)
def shared_agent_runner() -> AgentRunnerService:
    """La instancia del proceso. Los locks por conversación solo protegen si
    TODOS usan la misma: antes quotes.py y payment_agreement.py creaban la
    suya y podían escribir la sesión a la vez que un turno en curso."""
    return AgentRunnerService()
