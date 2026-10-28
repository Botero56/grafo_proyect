"""Memoria de la conversación: la sesión ADK guarda solo los últimos eventos
y un RESUMEN de lo anterior.

Sin esto la sesión del día crecía sin límite (810 KB y 179 eventos en una
prueba del 2026-10-05, el tope de Firestore es 1 MiB) y cada llamada al
modelo reenviaba toda la historia. Cuando la sesión pasa de MAX_EVENTS, el
runner (AgentRunnerService) resume lo viejo con una llamada corta al modelo
y deja solo los últimos turnos; el resumen viaja en la instrucción del
agente (with_conversation_summary). Sigue siendo una sesión por día: lo que
debe durar más (perfil, pedidos, menú) vive en Firestore y se lee con tools.
"""

import json
import logging
from typing import Callable, Optional

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.events.event import Event
from google.genai import types

from app.ai.core.genai_client import build_genai_client
from app.config import ai_restaurant_settings

logger = logging.getLogger(__name__)

SUMMARY_KEY = "resumen_conversacion"
# Se compacta al pasar de MAX_EVENTS y quedan unos KEEP_EVENTS (~4 turnos
# con tools: mensaje, llamadas, respuestas de tools, respuesta final).
MAX_EVENTS = 60
KEEP_EVENTS = 24
# Lo que se le pasa al modelo de cada respuesta de tool (algunas traen el
# menú entero); el resumen no necesita más.
_TOOL_RESULT_CHARS = 400

_SUMMARY_INSTRUCTIONS = """
Eres la memoria de un asistente de pedidos de un restaurante por WhatsApp.
Te paso el resumen anterior (si hay) y la parte vieja de la conversación de
hoy. Escribe un resumen NUEVO que la reemplace, en español, máximo 12 líneas
cortas, solo con hechos que sirvan para seguir atendiendo:
- datos del cliente que dio (nombre, dirección, preferencias);
- cada pedido de hoy: order_id, qué pidió, total, método y estado del pago;
- lo que quedó pendiente o prometido (ej. espera cotización de domicilio,
  espera respuesta de la administración, pidió hablar con una persona).
No inventes nada que no esté en el texto. Sin saludos ni explicaciones.
""".strip()


def with_conversation_summary(prompt: str) -> Callable[[ReadonlyContext], str]:
    """Instrucción del agente = prompt fijo + resumen de lo anterior, si hay.
    El estado del turno NO va aquí: ver append_current_state.

    Al ser una función, ADK no interpreta llaves {…} del prompt como
    variables de estado."""

    def instruction(ctx: ReadonlyContext) -> str:
        parts = [prompt]
        summary = (ctx.state.get(SUMMARY_KEY) or "").strip()
        if summary:
            parts.append(
                "Resumen de lo que ya pasó hoy en esta conversación "
                f"(los mensajes más viejos ya no están en el historial):\n{summary}"
            )
        return "\n\n".join(parts)

    return instruction


STATE_HEADER = "ESTADO ACTUAL (fuente de verdad: si el historial dice otra cosa, manda esto):\n"


def append_current_state(state_block: Callable[[dict], str]):
    """before_model_callback: el estado actual (ej. el pedido en curso) va AL
    FINAL de lo que se manda al modelo, recalculado en cada llamada.

    Antes iba en la instrucción del sistema, que es lo primero del request:
    como cambia en cada turno, rompía la caché implícita de Gemini y todo lo
    de después (tools e historial) se cobraba completo (38% de caché medido
    en oct-2026). Al final, instrucción, tools e historial se repiten
    idénticos entre llamadas y se cobran ~10x más barato. Solo se agrega al
    request: no queda en la sesión."""

    def callback(callback_context, llm_request) -> None:
        current = (state_block(callback_context.state) or "").strip()
        if current:
            llm_request.contents.append(types.Content(role="user", parts=[types.Part(text=STATE_HEADER + current)]))
        return None

    return callback


def split_point(events: list[Event]) -> Optional[int]:
    """Índice desde el que se conservan los eventos, o None si no hace falta
    compactar. Siempre corta al inicio de un turno del cliente, para no
    separar una llamada a una tool de su respuesta."""
    if len(events) <= MAX_EVENTS:
        return None
    for i in range(len(events) - KEEP_EVENTS, 0, -1):
        if events[i].author == "user":
            return i
    return None


def _render(events: list[Event]) -> str:
    lines = []
    for event in events:
        parts = (event.content.parts if event.content else None) or []
        for part in parts:
            if part.thought:
                continue
            if part.text:
                who = {"user": "Cliente", "human_agent": "Equipo"}.get(event.author, "Asistente")
                lines.append(f"{who}: {part.text.strip()}")
            elif part.function_call:
                lines.append(f"[tool {part.function_call.name}({json.dumps(part.function_call.args, ensure_ascii=False, default=str)})]")
            elif part.function_response:
                result = json.dumps(part.function_response.response, ensure_ascii=False, default=str)
                lines.append(f"[resultado {part.function_response.name}: {result[:_TOOL_RESULT_CHARS]}]")
            elif part.inline_data:
                lines.append("Cliente: [audio/imagen]")
    return "\n".join(lines)


async def summarize(previous_summary: Optional[str], events: list[Event]) -> str:
    """Resumen nuevo (anterior + eventos viejos). Lanza si el modelo falla:
    quien llama decide (el runner deja la sesión como estaba)."""
    text = (
        f"Resumen anterior:\n{previous_summary or '(no hay)'}\n\n"
        f"Conversación vieja:\n{_render(events)}"
    )
    client = build_genai_client()
    response = await client.aio.models.generate_content(
        model=ai_restaurant_settings.MODEL_RESTAURANT,
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=text)])],
        config=types.GenerateContentConfig(
            system_instruction=_SUMMARY_INSTRUCTIONS,
            max_output_tokens=600,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
        ),
    )
    summary = (response.text or "").strip()
    if not summary:
        raise ValueError("El modelo devolvió un resumen vacío")
    return summary
