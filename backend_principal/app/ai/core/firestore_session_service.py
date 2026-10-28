"""
SessionService de ADK respaldado en Firestore, para que el historial de una
conversación (turnos, tool calls) sobreviva un restart del proceso -- a
diferencia de InMemorySessionService (que usaba antes AgentRunnerService),
que pierde todo apenas el proceso se reinicia (deploy, crash, --reload de
uvicorn, o en Cloud Run: reciclado de instancia). Firestore sí guarda cada
`Messages` por separado, pero eso nunca se recargaba como contexto del
agente -- este SessionService es lo que cierra ese hueco.

Solo implementa el scope de estado "session" (sin prefijos app:/user:/temp:
de ADK) porque es el único que usa este proyecto hoy (ver agent_runner.py:
state={"business_id":..., "customer_id":...} y las tools que escriben
tool_context.state["..."] sin prefijo). Si en el futuro se necesita estado
compartido entre sesiones del mismo usuario (prefijo "user:") o global de la
app ("app:"), hay que agregar esa lógica -- ver
InMemorySessionService._merge_state en google-adk como referencia de
comportamiento.

Cada sesión completa (state + eventos) se guarda como UN documento Firestore
en adk_sessions/{app_name}/users/{user_id}/sessions/{session_id}, serializada
con Session.model_dump(mode="json") y guardada como un único campo JSON (para
no depender de que Firestore acepte cualquier forma anidada que produzca
pydantic). Firestore tiene un límite de 1MiB por documento (una sesión de
pruebas llegó a 810 KB en un día, 2026-10-05). Para no acercarse:

- Los audios/imágenes de un turno solo viajan al modelo en ESE turno: al
  guardar se reemplazan por un texto corto (_strip_media). 11 audios eran
  328 KB de esa sesión.
- AgentRunnerService resume la historia vieja cuando pasa de un tope y la
  reemplaza con replace_history (ver app/ai/core/conversation_memory.py).
- El documento se escribe una vez por turno, no por cada evento: antes cada
  tool call reescribía la sesión entera (~7 escrituras de cientos de KB por
  turno). Los eventos intermedios quedan pendientes en memoria y se
  escriben con el evento final o con flush_session() (el runner lo llama siempre al
  terminar el turno, también si falló, para no perder tools ya ejecutadas).

Usa el cliente ASYNC de google-cloud-firestore (no el mismo `fireo`/cliente
síncrono que usa el resto del proyecto) para no bloquear el event loop de
FastAPI en cada lectura/escritura de sesión.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.events.event import Event
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
    ListSessionsResponse,
)
from google.adk.sessions.session import Session
from google.cloud import firestore

from app.config import firestore_settings

logger = logging.getLogger(__name__)

_ROOT_COLLECTION = "adk_sessions"


# Lo que queda en la sesión en lugar de un audio/imagen ya respondido.
_MEDIA_PLACEHOLDER = "[{kind} del cliente, ya respondido]"


def _strip_media(data: dict) -> dict:
    """Session.model_dump(mode="json") sin los bytes de media de los eventos."""
    for event in data.get("events") or []:
        for i, part in enumerate(((event.get("content") or {}).get("parts")) or []):
            inline = part.get("inline_data") if isinstance(part, dict) else None
            if inline:
                mime = (inline.get("mime_type") or "").split("/")[0]
                kind = {"audio": "audio", "image": "imagen"}.get(mime, "archivo")
                event["content"]["parts"][i] = {"text": _MEDIA_PLACEHOLDER.format(kind=kind)}
    return data


class FirestoreSessionService(BaseSessionService):
    def __init__(self) -> None:
        self._client = firestore.AsyncClient(
            project=firestore_settings.PROJECT_ID,
            database=firestore_settings.DATABASE,
        )
        # Sesiones con eventos aún sin escribir (ver append_event/flush_session).
        self._dirty: dict[tuple[str, str, str], Session] = {}
        # Por sesión: escrituras y bytes del último guardado, para las
        # métricas del turno (AgentRunnerService las lee con pop_write_stats).
        self._write_stats: dict[str, dict[str, int]] = {}

    def _sessions_collection(self, app_name: str, user_id: str):
        return (
            self._client.collection(_ROOT_COLLECTION)
            .document(app_name)
            .collection("users")
            .document(user_id)
            .collection("sessions")
        )

    def _doc_ref(self, app_name: str, user_id: str, session_id: str):
        return self._sessions_collection(app_name, user_id).document(session_id)

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        session_id = (session_id or "").strip() or uuid.uuid4().hex
        doc_ref = self._doc_ref(app_name, user_id, session_id)

        existing = await doc_ref.get()
        if existing.exists:
            raise AlreadyExistsError(f"Session with id {session_id} already exists.")

        session = Session(
            app_name=app_name,
            user_id=user_id,
            id=session_id,
            state=state or {},
            last_update_time=time.time(),
        )
        await doc_ref.set(self._serialize(session))
        return session.model_copy(deep=True)

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        snap = await self._doc_ref(app_name, user_id, session_id).get()
        if not snap.exists:
            return None
        session = self._deserialize(snap.to_dict())

        if config:
            if config.num_recent_events is not None:
                if config.num_recent_events == 0:
                    session.events = []
                else:
                    session.events = session.events[-config.num_recent_events :]
            if config.after_timestamp:
                i = len(session.events) - 1
                while i >= 0:
                    if session.events[i].timestamp < config.after_timestamp:
                        break
                    i -= 1
                if i >= 0:
                    session.events = session.events[i + 1 :]

        return session

    async def list_sessions(
        self, *, app_name: str, user_id: Optional[str] = None
    ) -> ListSessionsResponse:
        sessions: list[Session] = []

        if user_id is not None:
            async for doc in self._sessions_collection(app_name, user_id).stream():
                s = self._deserialize(doc.to_dict())
                s.events = []
                sessions.append(s)
        else:
            users_ref = (
                self._client.collection(_ROOT_COLLECTION)
                .document(app_name)
                .collection("users")
            )
            async for user_doc in users_ref.stream():
                async for doc in user_doc.reference.collection("sessions").stream():
                    s = self._deserialize(doc.to_dict())
                    s.events = []
                    sessions.append(s)

        sessions.sort(key=lambda s: (s.last_update_time, s.user_id, s.id))
        return ListSessionsResponse(sessions=sessions)

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        await self._doc_ref(app_name, user_id, session_id).delete()

    async def append_event(self, session: Session, event: Event) -> Event:
        # El comportamiento base (aplicar el state_delta al objeto Session
        # en memoria y agregar el evento a session.events) es el mismo que
        # usa InMemorySessionService; acá solo se agrega la persistencia.
        event = await super().append_event(session=session, event=event)
        if event.partial:
            return event
        # El mensaje del cliente también cuenta como "final" para ADK: se
        # espera a la respuesta del agente para escribir una sola vez.
        if event.author != "user" and event.is_final_response():
            await self._write(session)
        else:
            self._dirty[(session.app_name, session.user_id, session.id)] = session
        return event

    # No se llama `flush`: BaseSessionService de ADK ya tiene un flush() sin
    # argumentos, y el runner terminaba llamando ese con tres.
    async def flush_session(self, app_name: str, user_id: str, session_id: str) -> None:
        """Escribe lo que quedó pendiente del turno (ej. tools ya ejecutadas
        de un turno que terminó en error o por timeout)."""
        session = self._dirty.get((app_name, user_id, session_id))
        if session is not None:
            await self._write(session)

    async def replace_history(self, session: Session, events: list[Event], state_updates: dict) -> None:
        """Deja solo `events` en la sesión y aplica `state_updates` (ej. el
        resumen de lo anterior). Lo usa la compactación del runner."""
        session.events = list(events)
        session.state.update(state_updates)
        await self._write(session)

    def pop_write_stats(self, session_id: str) -> dict[str, int]:
        return self._write_stats.pop(session_id, {"writes": 0, "bytes": 0, "events": None})

    async def _write(self, session: Session) -> None:
        payload = self._serialize(session)
        await self._doc_ref(session.app_name, session.user_id, session.id).set(payload)
        self._dirty.pop((session.app_name, session.user_id, session.id), None)
        stats = self._write_stats.setdefault(session.id, {"writes": 0, "bytes": 0, "events": None})
        stats["writes"] += 1
        stats["bytes"] = len(payload["data"].encode("utf-8"))
        stats["events"] = len(session.events)

    @staticmethod
    def _serialize(session: Session) -> dict[str, Any]:
        return {"data": json.dumps(_strip_media(session.model_dump(mode="json")))}

    @staticmethod
    def _deserialize(data: dict[str, Any]) -> Session:
        return Session.model_validate(json.loads(data["data"]))
