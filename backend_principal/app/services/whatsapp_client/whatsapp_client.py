"""Cliente genérico para la API de WhatsApp Cloud (Graph API de Meta).

No es solo "enviar texto": expone GET/POST genéricos contra Graph API y
helpers para los tipos de mensaje que usa el proyecto (texto, documento,
botones, plantillas), además de subir y descargar media.
Cualquier endpoint nuevo de Cloud API se puede usar con `get`/`post` sin
tener que volver a tocar esta clase.

Dos proveedores con la MISMA forma de API (rutas y payloads de Cloud API):

- "meta":  https://graph.facebook.com/<versión>, `Authorization: Bearer`.
- "kapso": https://api.kapso.ai/meta/whatsapp/<versión>, `X-API-Key` (ver
  https://docs.kapso.ai/api/introduction). Solo cambian la base y el header.
"""

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.services.phones import is_username_id
from app.config import whatsapp_settings
from app.services import secrets

logger = logging.getLogger("whatsapp-webhook")

# Negocios que ya avisaron que envían con la key global (un warning por
# proceso, no uno por mensaje).
_warned_env_fallback: set[str] = set()


def _warn_env_fallback(business_id: str) -> None:
    if business_id in _warned_env_fallback:
        return
    _warned_env_fallback.add(business_id)
    logger.warning(
        "Tenant sin secreto en Secret Manager; usando la variable de entorno KAPSO_API_KEY",
        extra={"fields": {"business_id": business_id, "secret_fallback": True}},
    )

# Respuestas que Meta no procesó y vale la pena reintentar, y la espera
# antes de cada reintento (dos reintentos como máximo).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_RETRY_DELAYS = (0.5, 2.0)


def _mask(phone: Optional[str]) -> str:
    """Teléfono para logs: solo los últimos 4 dígitos (es dato personal)."""
    return f"***{phone[-4:]}" if phone else "-"


PROVIDER_META = "meta"
PROVIDER_KAPSO = "kapso"


def provider_for(business) -> str:
    """Proveedor de envío del negocio: su `whatsapp_provider` o, si no lo
    tiene, el global WHATSAPP_PROVIDER. Cualquier valor desconocido cae a
    Meta, que es como funcionaba todo antes de Kapso."""
    provider = (getattr(business, "whatsapp_provider", None) or whatsapp_settings.WHATSAPP_PROVIDER or "")
    return PROVIDER_KAPSO if provider.strip().lower() == PROVIDER_KAPSO else PROVIDER_META


class WhatsAppClient:
    def __init__(
        self,
        token: str,
        phone_number_id: str,
        api_version: Optional[str] = None,
        timeout: float = 30.0,
        provider: str = PROVIDER_META,
        secret_ref: Optional[str] = None,
    ) -> None:
        self._token = token
        self._phone_number_id = phone_number_id
        self._provider = provider
        # Referencia en Secret Manager de `token`, si salió de ahí: ante un
        # 401 se relee una vez (puede que hayan rotado la key).
        self._secret_ref = secret_ref
        if provider == PROVIDER_KAPSO:
            self._api_version = api_version or whatsapp_settings.KAPSO_API_VERSION
            base = whatsapp_settings.KAPSO_API_BASE_URL.rstrip("/")
        else:
            self._api_version = api_version or whatsapp_settings.GRAPH_API_VERSION
            base = "https://graph.facebook.com"
        self._base_url = f"{base}/{self._api_version}"
        self._base_host = urlparse(self._base_url).hostname
        self._timeout = timeout

    @classmethod
    def for_business(cls, business) -> "WhatsAppClient":
        """Cliente que envía desde el número del negocio, o desde el
        PHONE_NUMBER_ID global si el negocio no tiene uno propio.

        Kapso: la API key de ESE negocio, de Secret Manager
        (`Businesses.kapso_api_key_secret`). Sin referencia, la global
        KAPSO_API_KEY con un warning (migración; se quita cuando todos los
        negocios tengan su secreto).

        Meta: el token del usuario del sistema (TEMP_TOKEN_WPP), que no
        vence. Ya no se leen tokens de Firestore (WhatsappCredentials)."""
        phone_number_id = business.whatsapp_phone_number_id or whatsapp_settings.PHONE_NUMBER_ID
        if provider_for(business) == PROVIDER_KAPSO:
            ref = getattr(business, "kapso_api_key_secret", None)
            if ref:
                # Falla cerrado: sin poder leer su key, el negocio no envía
                # (401 logueado) en vez de usar la de otro.
                token = secrets.get_secret_or_none(ref) or ""
            else:
                _warn_env_fallback(business.bussiness_id)
                token = whatsapp_settings.KAPSO_API_KEY
                if not token:
                    logger.error(
                        "El negocio %s envía por Kapso pero KAPSO_API_KEY está vacía",
                        business.bussiness_id,
                    )
            return cls(
                token=token,
                phone_number_id=phone_number_id,
                provider=PROVIDER_KAPSO,
                secret_ref=ref,
            )

        return cls(
            token=whatsapp_settings.TEMP_TOKEN_WPP,
            phone_number_id=phone_number_id,
        )

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def _headers(self) -> dict:
        if self._provider == PROVIDER_KAPSO:
            return {"X-API-Key": self._token}
        return {"Authorization": f"Bearer {self._token}"}

    def _headers_for(self, url: str) -> dict:
        """Headers de auth para `url`. Con Kapso, la API key solo viaja a su
        propio host: una URL absoluta de otro dominio (ej. una URL de media
        firmada) no la recibe. Con Meta se mantiene el comportamiento de
        siempre: la URL de descarga de media (lookaside.fbsbx.com) exige el
        mismo Bearer."""
        if self._provider == PROVIDER_KAPSO and urlparse(url).hostname != self._base_host:
            return {}
        return self._headers

    def _resolve_url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self._base_url}/{path.lstrip('/')}"

    @staticmethod
    def _unwrap(result: Optional[dict], key: str) -> Optional[dict]:
        """Kapso envuelve algunas respuestas en `{"data": {...}}`; Graph API
        no. Si `key` no está arriba pero sí dentro de `data`, devuelve
        `data`; si no, la respuesta tal cual."""
        if not isinstance(result, dict) or key in result:
            return result
        data = result.get("data")
        return data if isinstance(data, dict) and key in data else result

    # ------------------------------------------------------------------
    # HTTP genérico: cualquier endpoint de Graph API que no tenga helper acá.
    # ------------------------------------------------------------------

    async def get(self, path: str, params: Optional[dict] = None) -> httpx.Response:
        return await self._request("GET", path, params=params)

    async def post(
        self,
        path: str,
        json: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
    ) -> httpx.Response:
        return await self._request("POST", path, json=json, data=data, files=files)

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = self._resolve_url(path)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(method, url, headers=self._headers_for(url), **kwargs)
            if response.status_code == 401 and self._refresh_secret():
                response = await client.request(method, url, headers=self._headers_for(url), **kwargs)
            return response

    def _refresh_secret(self) -> bool:
        """Tras un 401: relee la key de Secret Manager. True solo si cambió
        (la rotaron); si es la misma, el 401 es real y no se reintenta."""
        if not self._secret_ref:
            return False
        secrets.invalidate(self._secret_ref)
        fresh = secrets.get_secret_or_none(self._secret_ref)
        if not fresh or fresh == self._token:
            return False
        logger.info("La API key del negocio cambió en Secret Manager; se reintenta con la nueva")
        self._token = fresh
        return True

    # ------------------------------------------------------------------
    # Envío de mensajes
    # ------------------------------------------------------------------

    async def _send(self, payload: dict) -> Optional[dict]:
        """Respuesta de Graph API, o None si el envío falló (ya logueado).

        Reintenta solo lo que Meta no procesó: 429/5xx y errores de
        conexión. Un timeout de lectura NO se reintenta: el mensaje pudo
        haber salido y el cliente lo recibiría dos veces."""
        to = payload.get("to")
        if is_username_id(to):
            # Cliente con nombre de usuario: Meta recibe su BSUID en `recipient`.
            payload = {**{k: v for k, v in payload.items() if k != "to"}, "recipient": to}
        for delay in (*_RETRY_DELAYS, None):
            try:
                response = await self.post(
                    f"{self._phone_number_id}/messages",
                    json={"messaging_product": "whatsapp", **payload},
                )
            except httpx.ConnectError:
                if delay is not None:
                    await asyncio.sleep(delay)
                    continue
                logger.exception("WhatsApp: no se pudo conectar para enviar a %s", _mask(to))
                return None
            except httpx.HTTPError:
                logger.exception("WhatsApp: error de red enviando a %s (no se reintenta)", _mask(to))
                return None

            if response.status_code == 200:
                return response.json()
            if response.status_code in _RETRYABLE_STATUS and delay is not None:
                await asyncio.sleep(delay)
                continue
            self._log_api_error("enviar mensaje", response, to)
            return None
        return None  # pragma: no cover -- el último intento siempre retorna arriba

    def _log_api_error(self, action: str, response: httpx.Response, to: Optional[str] = None) -> None:
        """Una línea con lo necesario para diagnosticar: código HTTP, código
        de Meta (190 = token vencido, 131005 = permisos, 131047 = fuera de
        la ventana de 24 h...) y desde qué número se envió."""
        try:
            error = response.json().get("error") or {}
        except Exception:
            error = {}
        if not isinstance(error, dict):
            # Kapso responde `{"error": "texto"}`, no el objeto de Meta.
            error = {"message": str(error)}
        logger.error(
            "WhatsApp (%s) rechazó %s: http=%s code=%s subcode=%s phone_number_id=%s to=%s: %s",
            self._provider,
            action,
            response.status_code,
            error.get("code"),
            error.get("error_subcode"),
            self._phone_number_id,
            _mask(to),
            error.get("message") or response.text[:300],
        )

    async def send_message(self, to: str, text: str) -> bool:
        return await self._send({"to": to, "type": "text", "text": {"body": text}}) is not None

    async def send_text_and_get_id(self, to: str, text: str) -> Optional[str]:
        """Como send_message, pero devuelve el wamid asignado por WhatsApp
        (o None si falló), para poder guardarlo en Messages.whatsapp_message_id."""
        return self._first_message_id(
            await self._send({"to": to, "type": "text", "text": {"body": text}})
        )

    @staticmethod
    def _first_message_id(result: Optional[dict]) -> Optional[str]:
        """wamid que asignó WhatsApp al mensaje enviado, o None si falló."""
        result = WhatsAppClient._unwrap(result, "messages")
        if not result:
            return None
        messages = result.get("messages") or []
        return messages[0].get("id") if messages else None

    async def send_document(
        self,
        to: str,
        *,
        link: Optional[str] = None,
        media_id: Optional[str] = None,
        filename: Optional[str] = None,
        caption: Optional[str] = None,
    ) -> bool:
        document = self._media_ref(link, media_id)
        if filename:
            document["filename"] = filename
        if caption:
            document["caption"] = caption
        return await self._send({"to": to, "type": "document", "document": document}) is not None

    async def send_interactive_buttons(
        self, to: str, body_text: str, buttons: list[dict[str, str]]
    ) -> Optional[str]:
        """Manda un mensaje con hasta 3 botones de respuesta rápida.

        `buttons`: lista de {"id": ..., "title": ...} (WhatsApp limita el
        título a 20 caracteres y a un máximo de 3 botones). Devuelve el
        wamid asignado, o None si falló.
        """
        result = await self._send(
            {
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body_text},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                            for b in buttons
                        ]
                    },
                },
            }
        )
        return self._first_message_id(result)

    async def send_interactive_list(
        self, to: str, body_text: str, button_label: str, rows: list[dict[str, str]]
    ) -> Optional[str]:
        """Mensaje con una lista desplegable de hasta 10 opciones.

        `rows`: [{"id": ..., "title": ..., "description"?: ...}]. WhatsApp
        limita el título a 24 caracteres, la descripción a 72 y el botón que
        abre la lista a 20. La respuesta llega como interactive.list_reply.
        Devuelve el wamid, o None si falló."""
        result = await self._send(
            {
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": body_text},
                    "action": {
                        "button": button_label[:20],
                        "sections": [{
                            "title": "Opciones",
                            "rows": [
                                {"id": r["id"], "title": r["title"][:24], **({"description": r["description"][:72]} if r.get("description") else {})}
                                for r in rows[:10]
                            ],
                        }],
                    },
                },
            }
        )
        return self._first_message_id(result)

    async def send_template(
        self, to: str, template_name: str, language_code: str = "es", params: Optional[list[str]] = None
    ) -> bool:
        """Manda un mensaje de plantilla aprobada. Necesario para iniciar
        una conversación fuera de la ventana de 24h (ej. recordatorio diario
        de menú, ver app/services/daily_menu/reminders.py) -- WhatsApp
        rechaza mensajes de texto libre iniciados por el negocio pasada esa
        ventana. `params`: valores de {{1}}, {{2}}... del cuerpo, en orden."""
        template = {"name": template_name, "language": {"code": language_code}}
        if params:
            template["components"] = [
                {"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}
            ]
        return await self._send({"to": to, "type": "template", "template": template}) is not None

    @staticmethod
    def _media_ref(link: Optional[str], media_id: Optional[str]) -> dict:
        if not link and not media_id:
            raise ValueError("send_* de media requiere 'link' o 'media_id'")
        return {"link": link} if link else {"id": media_id}

    async def mark_read(self, message_id: str) -> bool:
        """Marca el mensaje como leído y muestra "escribiendo..." al cliente
        (se quita solo con la respuesta, o a los 25 s)."""
        payload = {"status": "read", "message_id": message_id, "typing_indicator": {"type": "text"}}
        return await self._send(payload) is not None

    async def send_image(self, to: str, media_id: str, caption: Optional[str] = None) -> bool:
        image: dict = {"id": media_id}
        if caption:
            image["caption"] = caption
        return await self._send({"to": to, "type": "image", "image": image}) is not None

    # ------------------------------------------------------------------
    # Media: subir (para enviar por media_id) y descargar (media entrante)
    # ------------------------------------------------------------------

    async def upload_media(
        self, file_bytes: bytes, filename: str, mime_type: str
    ) -> Optional[str]:
        response = await self.post(
            f"{self._phone_number_id}/media",
            data={"messaging_product": "whatsapp", "type": mime_type},
            files={"file": (filename, file_bytes, mime_type)},
        )
        if response.status_code == 200:
            return (self._unwrap(response.json(), "id") or {}).get("id")
        self._log_api_error("subir media", response)
        return None

    async def get_media_metadata(self, media_id: str) -> Optional[dict[str, Any]]:
        """GET /{media-id}: incluye la url temporal (~5 min) para descargar.

        Kapso exige `?phone_number_id=` (sin él responde 404 "WhatsApp
        configuration not found") y devuelve dos URLs: `url` (lookaside de
        Meta, que pide un token de Meta que con Kapso no tenemos) y
        `download_url` (de Kapso, firmada). Se deja la de Kapso en `url`
        para que quien consuma la metadata no tenga que saber del proveedor."""
        params = {"phone_number_id": self._phone_number_id} if self._provider == PROVIDER_KAPSO else None
        response = await self.get(media_id, params=params)
        if response.status_code != 200:
            self._log_api_error("obtener metadata de media", response)
            return None

        metadata = self._unwrap(response.json(), "id")
        if self._provider == PROVIDER_KAPSO and isinstance(metadata, dict) and metadata.get("download_url"):
            metadata = {**metadata, "meta_url": metadata.get("url"), "url": metadata["download_url"]}
        return metadata

    async def download_media(self, media_id: str) -> Optional[bytes]:
        """Descarga el contenido de un media entrante (imagen, audio, doc)."""
        metadata = await self.get_media_metadata(media_id)
        media_url = metadata.get("url") if metadata else None
        if not media_url:
            return None

        response = await self.get(media_url)
        if response.status_code == 200:
            return response.content
        self._log_api_error("descargar media", response)
        return None
