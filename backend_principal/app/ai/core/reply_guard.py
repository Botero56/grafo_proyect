"""Red de seguridad sobre el texto final que un agente ADK va a mandarle a
un cliente o admin, para el caso en que el modelo "alucina" y dicho texto
final (no un thought part -- ver el filtro `not part.thought` en
agent_runner.py, ese es el fix de raíz) igual queda largo o con muletillas
de razonamiento en primera persona ("necesito verificar...", "el usuario
quiere...", menciones a tools/IDs internos, etc.) en vez de ser solo la
respuesta útil para la persona que espera el mensaje.

Es deliberadamente barato: solo dispara un segundo llamado al modelo cuando
el texto pinta sospechoso (heurística en `_looks_leaked`), no en cada turno.
"""

import logging
import re

from google.genai import types

from app.ai.core.genai_client import build_genai_client
from app.config import ai_restaurant_settings

logger = logging.getLogger(__name__)

_MAX_CHARS = 700

_LEAK_PATTERNS = re.compile(
    r"\b("
    r"necesito (verificar|revisar|confirmar|pensar)|"
    r"voy a (revisar|pensar|analizar|verificar)|"
    r"(analicemos|pensemos|repasemos)|"
    r"paso \d|primero,? debo|"
    r"el (usuario|cliente) (quiere|pregunta|pide|dice)|"
    r"razonamiento|nota interna|"
    r"como (ia|modelo|asistente de ia|llm)\b|"
    r"function_call|tool_context|"
    r"\bthought\b"
    r")",
    re.IGNORECASE,
)

# El modelo hablando de cómo funciona (prompt injection que funcionó a
# medias: "no puedo listar mis herramientas" ya confirma que existen). No se
# reescribe con el modelo: se cambia por la redirección fija, sin costo.
_CONFIG_LEAK = re.compile(
    r"\b("
    r"mis (instrucciones|herramientas|funciones|reglas)|"
    r"herramientas (internas|con (las )?que)|"
    r"instrucciones (internas|del sistema)|"
    r"informaci[oó]n (t[eé]cnica|interna)|"
    r"configuraci[oó]n (interna|del sistema)|"
    r"prompt|system prompt|"
    r"fui (cread[oa]|programad[oa]|entrenad[oa]|configurad[oa])"
    r")",
    re.IGNORECASE,
)
OFF_TOPIC_REPLY = "Con eso no te puedo ayudar 🙂 ¿Te ayudo con algo del restaurante?"

_SUMMARY_INSTRUCTIONS = """
Eres un editor que revisa mensajes de WhatsApp de un bot de restaurante antes
de que salgan hacia un cliente o un administrador. Te llega un borrador que
puede tener razonamiento interno filtrado (dudas, pasos, menciones a
funciones/IDs internos) en vez de ser solo la respuesta final.

Devuelve SOLO la respuesta final útil para quien va a leer el mensaje, en
máximo 4 líneas cortas, tono directo y natural en español, sin markdown.
Quita cualquier razonamiento, duda, mención a "IA"/modelo, nombres de
funciones o tools, IDs internos o pasos de análisis. No inventes datos que
no estén en el borrador. Si el borrador no trae ninguna respuesta real (solo
razonamiento, sin conclusión), responde exactamente:
"Dame un momento, ya te confirmo."
""".strip()


def _looks_leaked(text: str) -> bool:
    return len(text) > _MAX_CHARS or bool(_LEAK_PATTERNS.search(text))


_MARKDOWN_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)


def to_whatsapp_format(text: str) -> str:
    """**negrita** de markdown -> *negrita* de WhatsApp. El modelo escribe
    markdown aunque el prompt diga que no, y WhatsApp muestra los dobles
    asteriscos tal cual (prueba 2026-10-05)."""
    return _MARKDOWN_BOLD.sub(r"*\1*", text)


async def guard_reply(text: str) -> str:
    """Si `text` parece razonamiento filtrado o es demasiado largo, pide un
    resumen aparte al modelo. Best-effort: cualquier fallo del resumen cae a
    un truncado simple en vez de mandar el texto crudo sin control."""
    if not text:
        return text
    if _CONFIG_LEAK.search(text):
        logger.warning("Respuesta reemplazada: hablaba de la configuración interna del bot")
        return OFF_TOPIC_REPLY
    if not _looks_leaked(text):
        return text

    try:
        client = build_genai_client()
        response = await client.aio.models.generate_content(
            model=ai_restaurant_settings.MODEL_RESTAURANT,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=text)])],
            config=types.GenerateContentConfig(
                system_instruction=_SUMMARY_INSTRUCTIONS,
                temperature=0.2,
                max_output_tokens=200,
            ),
        )
        summary = (response.text or "").strip()
        if summary:
            logger.warning(
                "Respuesta del agente resumida por parecer razonamiento filtrado "
                "(largo original=%d)",
                len(text),
            )
            return summary
    except Exception:
        logger.exception("Fallo resumiendo una respuesta que parecía razonamiento filtrado")

    return text[:_MAX_CHARS].rstrip() + "…"
