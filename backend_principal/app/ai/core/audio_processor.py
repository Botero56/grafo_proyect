"""Descarga, valida y transcribe las notas de voz entrantes de WhatsApp.

La transcripción entra al flujo como un mensaje de texto más (ver
inbound_pipeline.flush_conversation): la IA nunca recibe el audio crudo. La
validación de duración cubre el caso real de WhatsApp (notas de voz en
OGG/Opus); otros formatos de audio poco comunes (ej. AMR) no los soporta
`mutagen` y se rechazan.
"""

import io
import logging
from dataclasses import dataclass
from typing import Optional

from google.genai import types
from mutagen import File as MutagenFile

from app.ai.core.genai_client import build_genai_client
from app.config import ai_restaurant_settings
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient

logger = logging.getLogger("whatsapp-webhook")

MAX_AUDIO_SECONDS = 60

_TRANSCRIBE_INSTRUCTIONS = """
Transcribe literalmente esta nota de voz de un cliente de un negocio por
WhatsApp, en el idioma en que habla. Solo el texto dicho, sin comentarios,
sin resumir y sin corregir lo que dijo (si se corrige a sí mismo, "no, espera,
mejor...", transcríbelo tal cual). Marca con [inaudible] cada parte que no se
entienda. Si no se entiende nada, responde solo: [inaudible]
""".strip()


class AudioProcessingError(Exception):
    """El audio no se pudo descargar, leer, o excede la duración máxima."""


@dataclass
class ProcessedAudio:
    mime_type: str
    duration_seconds: float
    data: bytes


class AudioProcessor:
    """Descarga un audio de WhatsApp Cloud API por su media_id y valida que
    dure como máximo `max_seconds`."""

    def __init__(
        self,
        whatsapp_client: WhatsAppClient,
        max_seconds: int = MAX_AUDIO_SECONDS,
    ) -> None:
        self._whatsapp_client = whatsapp_client
        self._max_seconds = max_seconds

    async def process(self, media_id: str, mime_type: Optional[str] = None) -> ProcessedAudio:
        data = await self._whatsapp_client.download_media(media_id)
        if not data:
            raise AudioProcessingError(f"No se pudo descargar el audio {media_id}")

        resolved_mime = (mime_type or "audio/ogg").split(";")[0].strip()

        duration = self._get_duration_seconds(data)
        if duration is None:
            raise AudioProcessingError(
                f"No se pudo determinar la duración del audio {media_id} "
                f"(mime_type={resolved_mime})"
            )
        if duration > self._max_seconds:
            raise AudioProcessingError(
                f"Audio {media_id} dura {duration:.1f}s, excede el máximo "
                f"permitido de {self._max_seconds}s"
            )

        return ProcessedAudio(mime_type=resolved_mime, duration_seconds=duration, data=data)

    @staticmethod
    def _get_duration_seconds(data: bytes) -> Optional[float]:
        try:
            audio = MutagenFile(io.BytesIO(data))
            if audio is None or audio.info is None:
                return None
            return float(audio.info.length)
        except Exception:
            logger.exception("No se pudo leer la metadata del audio")
            return None


async def transcribe(audio: ProcessedAudio) -> str:
    """Texto de la nota de voz (una llamada corta al modelo, solo en turnos
    con audio). Lanza AudioProcessingError si el modelo no devuelve nada."""
    response = await build_genai_client().aio.models.generate_content(
        model=ai_restaurant_settings.MODEL_RESTAURANT,
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_bytes(data=audio.data, mime_type=audio.mime_type)],
            )
        ],
        config=types.GenerateContentConfig(
            system_instruction=_TRANSCRIBE_INSTRUCTIONS,
            temperature=0,
            max_output_tokens=600,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
        ),
    )
    text = (response.text or "").strip()
    if not text:
        raise AudioProcessingError("El modelo no devolvió transcripción")
    return text
