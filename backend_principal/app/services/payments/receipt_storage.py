"""Guarda en Cloud Storage la imagen de cada comprobante de transferencia,
para saber exactamente qué envió el cliente si después hay un reclamo.

Ruta: {business_id}/{customer_id}/{YYYY-MM-DD}/{order_id}_{HHMMSS}.{ext},
con la fecha y hora de Bogotá. En Firestore solo se guarda la URL (ver
Orders.transfer_receipt_urls), que es lo que consulta la IA del admin.

El bucket es privado: la URL abre la imagen solo para quien tenga acceso al
proyecto (iniciando sesión con su cuenta de Google), no es un enlace público.
"""

import logging
import mimetypes
from datetime import datetime
from functools import lru_cache
from typing import Optional

from google.cloud import storage

from app.config import storage_settings
from app.services.excel_report_service.constants import DEFAULT_TZ

logger = logging.getLogger("whatsapp-webhook")


@lru_cache(maxsize=1)
def _client() -> storage.Client:
    return storage.Client()


def receipt_path(business_id: str, customer_id: str, order_id: str, mime_type: str, now: datetime) -> str:
    extension = mimetypes.guess_extension(mime_type) or ".jpg"
    return f"{business_id}/{customer_id}/{now:%Y-%m-%d}/{order_id}_{now:%H%M%S}{extension}"


def store_receipt(
    business_id: str, customer_id: str, order_id: str, image: bytes, mime_type: str
) -> Optional[str]:
    """URL del comprobante guardado, o None si no se pudo guardar (ya
    logueado). Nunca lanza: perder la copia no debe frenar la validación del
    pago. Bloqueante: usar con run_in_threadpool."""
    bucket = storage_settings.RECEIPTS_BUCKET
    if not bucket:
        logger.warning("RECEIPTS_BUCKET sin configurar: el comprobante del pedido %s no se guarda", order_id)
        return None
    path = receipt_path(business_id, customer_id, order_id, mime_type, datetime.now(DEFAULT_TZ))
    try:
        _client().bucket(bucket).blob(path).upload_from_string(image, content_type=mime_type, timeout=30)
    except Exception:
        logger.exception("No se pudo guardar el comprobante del pedido %s en %s", order_id, bucket)
        return None
    return f"https://storage.cloud.google.com/{bucket}/{path}"
