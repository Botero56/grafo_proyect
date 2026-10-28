"""Validación de la firma de los webhooks entrantes (Meta directo o Kapso).

Meta firma el body crudo del POST con HMAC-SHA256 usando el App Secret (no
el token de acceso). Hay que validar contra el body EXACTO tal como llegó
por la red -- si se recalcula sobre un dict re-serializado (json.dumps del
body ya parseado) la firma puede no coincidir por diferencias de espaciado/
orden de claves, por eso `verify_meta_signature` recibe bytes crudos, no un
dict.

Kapso reenvía el payload de Meta tal cual (webhook `kind: "meta"`) pero lo
firma él: HMAC-SHA256 hex del body crudo con el `secret_key` del webhook,
en `X-Webhook-Signature` y SIN el prefijo `sha256=`.

Ver: https://developers.facebook.com/docs/graph-api/webhooks/getting-started#verify-payloads
     https://docs.kapso.ai/docs/platform/webhooks/security
"""

import hashlib
import hmac
from typing import Mapping

META_SIGNATURE_HEADER = "X-Hub-Signature-256"
KAPSO_SIGNATURE_HEADER = "X-Webhook-Signature"


def _hmac_hex(secret: str, raw_body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_meta_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """True si `signature_header` (valor del header X-Hub-Signature-256)
    coincide con el HMAC-SHA256 de `raw_body` firmado con `app_secret`.

    Falta el header o el secreto, formato inválido, o no coincide -> False.
    Sin secreto configurado se rechaza: un HMAC con clave vacía lo puede
    calcular cualquiera.
    """
    if not app_secret or not signature_header or not signature_header.startswith("sha256="):
        return False

    received_digest = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(received_digest, _hmac_hex(app_secret, raw_body))


def verify_kapso_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Igual que verify_meta_signature pero con el formato de Kapso: hex
    sin prefijo. Se tolera un `sha256=` por si Kapso lo agrega."""
    if not secret or not signature_header:
        return False

    received_digest = signature_header.strip().removeprefix("sha256=")
    return hmac.compare_digest(received_digest, _hmac_hex(secret, raw_body))


def verify_webhook_signature(
    raw_body: bytes,
    headers: Mapping[str, str],
    *,
    app_secret: str,
    kapso_secret: str,
) -> bool:
    """Valida un webhook venga de Meta o de Kapso, según el header que trae.

    Durante la migración el mismo endpoint recibe de los dos: los números
    que siguen en la app de Meta propia y los que ya se conectaron por
    Kapso. Cada vía tiene su propio secreto, así que aceptar las dos no
    abre nada: una firma solo pasa si se hizo con el secreto de su vía.
    `headers` debe ser case-insensitive (el de Starlette lo es)."""
    kapso_header = headers.get(KAPSO_SIGNATURE_HEADER)
    if kapso_header is not None:
        return verify_kapso_signature(raw_body, kapso_header, kapso_secret)
    return verify_meta_signature(raw_body, headers.get(META_SIGNATURE_HEADER), app_secret)
