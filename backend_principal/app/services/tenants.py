"""Webhook por tenant: cada negocio registra en Kapso su propia URL,
`/webhook/kapso/{tenantKey}`. La clave solo elige qué secreto usar para
verificar la firma; la seguridad es la firma HMAC, no que la URL sea secreta.

Este módulo lo importa el webhook liviano: nada de fireo ni del worker.
Firestore se lee con el cliente oficial, importado en la primera llamada.
"""

import re
import secrets as _random
import threading
import time
from dataclasses import dataclass
from typing import Optional

from app.config import firestore_settings, whatsapp_settings

# Un acierto se recuerda 60 s (es lo que tarda en morir una clave rotada); un
# fallo 30 s, para que un escáner no cueste una lectura de Firestore por request.
_HIT_TTL_SECONDS = 60
_MISS_TTL_SECONDS = 30
# shortcut: al llenarse se vacía entera; basta contra claves basura, upgrade a LRU si molesta.
_MAX_CACHED = 1000
_KEY_FORMAT = re.compile(r"[a-z0-9]{1,12}_[A-Za-z0-9_-]{32}")

_client = None
_lock = threading.Lock()
_cache: dict[str, tuple[Optional["Tenant"], float]] = {}


@dataclass(frozen=True)
class Tenant:
    business_id: str
    phone_number_id: Optional[str]
    webhook_secret_ref: Optional[str]


def new_tenant_key(business_name: str) -> str:
    """`{slug}_{aleatorio}`: el slug es solo para reconocerla a ojo en los
    logs; los 24 bytes aleatorios (192 bits) son los que la hacen
    inadivinable."""
    slug = re.sub(r"[^a-z0-9]", "", (business_name or "").lower())[:12] or "tenant"
    return f"{slug}_{_random.token_urlsafe(24)}"


def webhook_url(tenant_key: str) -> str:
    return f"{whatsapp_settings.WEBHOOK_PUBLIC_BASE_URL.rstrip('/')}/webhook/kapso/{tenant_key}"


def _firestore():
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                from google.cloud import firestore

                _client = firestore.Client(
                    project=firestore_settings.PROJECT_ID, database=firestore_settings.DATABASE
                )
    return _client


def _lookup(tenant_key: str) -> Optional[Tenant]:
    from google.cloud.firestore_v1.base_query import FieldFilter

    docs = (
        _firestore()
        .collection("businesses")
        .where(filter=FieldFilter("webhook_tenant_key", "==", tenant_key))
        .limit(1)
        .get(timeout=5)
    )
    if not docs:
        return None
    data = docs[0].to_dict() or {}
    return Tenant(
        business_id=docs[0].id,
        phone_number_id=data.get("whatsapp_phone_number_id"),
        webhook_secret_ref=data.get("kapso_webhook_secret_secret"),
    )


def resolve_tenant(tenant_key: str) -> Optional[Tenant]:
    """Negocio dueño de la clave, o None. Bloqueante (Firestore): llamar
    con `run_in_threadpool`. Lanza si Firestore falla (el webhook responde
    503 y Kapso reintenta)."""
    if not _KEY_FORMAT.fullmatch(tenant_key or ""):
        return None  # ni se consulta: no puede ser una clave nuestra
    now = time.monotonic()
    cached = _cache.get(tenant_key)
    if cached is not None and cached[1] > now:
        return cached[0]
    tenant = _lookup(tenant_key)
    if len(_cache) >= _MAX_CACHED:
        _cache.clear()
    _cache[tenant_key] = (tenant, now + (_HIT_TTL_SECONDS if tenant else _MISS_TTL_SECONDS))
    return tenant


def clear_cache() -> None:
    _cache.clear()
