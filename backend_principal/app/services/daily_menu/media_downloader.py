"""Descarga y valida la imagen del menú del día enviada por un admin.

Mismo patrón que app.ai.core.audio_processor.AudioProcessor: pide la
metadata por media_id (GET /{media_id}, incluye url/mime_type/sha256/
file_size), descarga esa url con el mismo token, y guarda en disco. Acá
además se verifica el sha256 que reporta la Graph API contra el contenido
descargado (punto 2 del flujo) -- AudioProcessor no lo hace porque valida
duración en su lugar.
"""

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import daily_menu_settings
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient

logger = logging.getLogger("whatsapp-webhook")

MEDIA_DIR = Path(__file__).resolve().parent / "media"

_EXTENSION_BY_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


class MenuImageDownloadError(Exception):
    """La imagen no se pudo descargar, verificar, o excede el tamaño máximo."""


@dataclass
class DownloadedMenuImage:
    file_path: Path
    mime_type: str
    sha256: str
    data: bytes


class MenuImageDownloader:
    def __init__(self, whatsapp_client: WhatsAppClient) -> None:
        self._whatsapp_client = whatsapp_client
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    async def download(self, media_id: str) -> DownloadedMenuImage:
        metadata = await self._whatsapp_client.get_media_metadata(media_id)
        if not metadata or not metadata.get("url"):
            raise MenuImageDownloadError(f"No se pudo obtener metadata de {media_id}")

        file_size = metadata.get("file_size") or 0
        max_bytes = daily_menu_settings.MAX_MENU_IMAGE_SIZE_MB * 1024 * 1024
        if file_size and file_size > max_bytes:
            raise MenuImageDownloadError(
                f"Imagen {media_id} pesa {file_size} bytes, excede el máximo "
                f"permitido de {max_bytes} bytes"
            )

        response = await self._whatsapp_client.get(metadata["url"])
        if response.status_code != 200:
            raise MenuImageDownloadError(f"No se pudo descargar la imagen {media_id}")
        data = response.content

        expected_sha256 = metadata.get("sha256")
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if expected_sha256 and expected_sha256 != actual_sha256:
            raise MenuImageDownloadError(
                f"sha256 no coincide para {media_id} (esperado {expected_sha256}, "
                f"obtenido {actual_sha256})"
            )

        mime_type = (metadata.get("mime_type") or "image/jpeg").split(";")[0].strip()
        file_path = self._save_to_disk(media_id, mime_type, data)

        return DownloadedMenuImage(
            file_path=file_path, mime_type=mime_type, sha256=actual_sha256, data=data
        )

    def _save_to_disk(self, media_id: str, mime_type: str, data: bytes) -> Path:
        extension = _EXTENSION_BY_MIME.get(mime_type, "bin")
        file_path = MEDIA_DIR / f"{media_id}.{extension}"
        file_path.write_bytes(data)
        return file_path
