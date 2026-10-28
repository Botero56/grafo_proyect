"""Mensajes de domiciliarios: vía rápida determinística, sin IA.

Un domiciliario es un teléfono de Configurations.couriers del negocio (el
role_resolver lo resuelve a "domiciliario"). Si el parser entiende su
mensaje o nota de voz ("liquidar 12 15 base 50000", "entregué el pedido 18
con base de 75 mil"), se liquida acá sin llamar al modelo. Si no, sigue al
agente de domiciliarios (app/ai/ai_restaurant/courier), o al de clientes si
hoy eligió escribir como cliente. Las dos vías liquidan con `settle_and_report`.
"""

import logging
import re
import unicodedata
from typing import Optional

from starlette.concurrency import run_in_threadpool

from app.ai.core.audio_processor import AudioProcessingError, AudioProcessor, transcribe
from app.firestore_db.firebase_schemas import Businesses, Configurations, Customers, Messages
from app.schemas.enum_schemas import DeliveryEventSource, MessageOrigin
from app.services.conversation.handoff_service import local_phone
from app.services.daily_menu.menu_diff import format_cop
from app.services.delivery import incidents, tracking
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services.messaging.outbound_message_service import OutboundMessageService
from app.services.phones import normalize_phone
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient

logger = logging.getLogger("whatsapp-webhook")

_VERBS = {
    "liquidar": DeliveryEventSource.LIQUIDACION.value,
    "liquido": DeliveryEventSource.LIQUIDACION.value,
    "liquidacion": DeliveryEventSource.LIQUIDACION.value,
    "entregue": DeliveryEventSource.CONFIRMACION_TIEMPO_REAL.value,
    "entregado": DeliveryEventSource.CONFIRMACION_TIEMPO_REAL.value,
    "entregados": DeliveryEventSource.CONFIRMACION_TIEMPO_REAL.value,
}
# Palabras de relleno que no cambian el comando ("liquidar los pedidos 12 y 15",
# "entregué el pedido 1 con base de 75 mil").
_FILLER = {
    "y", "e", "el", "la", "los", "las", "pedido", "pedidos", "comanda", "comandas",
    "numero", "numeros", "no", "nro", "con", "de", "del", "ya", "hoy",
}
# Saludos o muletillas antes del verbo: no cambian el comando.
_LEADING = {"ya", "hola", "listo", "bueno", "buenas", "ok", "okay", "oye", "eh", "pues", "jefe"}
# "base 50000", "base $50.000", "base de 75 mil", "con una base de 75k".
_BASE = re.compile(r"\bbase\s*(?:de\s*)?\$?\s*(\d[\d.,]*)\s*(mil\b|k\b)?")
_MAX_CODE = 999
# Palabras que delatan una novedad de entrega ("no me abren el 5").
_INCIDENT_WORDS = {
    "problema", "novedad", "inconveniente", "abren", "abre", "contesta", "contestan",
    "responde", "responden", "porteria", "timbre",
}
# Respuesta al domiciliario si su agente falla (ej. 429): la vía rápida
# sigue funcionando sin el modelo.
HELP_TEXT = (
    "No pude procesar tu mensaje. Para liquidar escribe o di, por ejemplo:\n"
    "• liquidar 1 2 3 base 50000\n"
    "• entregué el pedido 4"
)


def _plain(text: str) -> str:
    """Minúsculas y sin tildes: "Entregué" -> "entregue"."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def parse_courier_command(text: str) -> Optional[dict]:
    """"liquidar 12, 15 #18 base 50.000" -> {"source": "liquidacion",
    "codes": [12, 15, 18], "base": 50000}. None si no es un comando claro
    (palabras de más, sin códigos): entonces lo atiende el agente."""
    plain = _plain(text).strip()
    base = None
    match = _BASE.search(plain)
    if match:
        base = int(re.sub(r"\D", "", match.group(1)) or 0) * (1000 if match.group(2) else 1)
        plain = plain[: match.start()] + " " + plain[match.end():]
    tokens = re.sub(r"[#,;.:\-!¡]", " ", plain).split()
    while tokens and tokens[0] in _LEADING:  # "Ya entregué el 7", "Hola, liquidar 3"
        tokens = tokens[1:]
    if not tokens or tokens[0] not in _VERBS:
        return None
    codes = []
    for token in tokens[1:]:
        if token.isdigit() and 0 < int(token) <= _MAX_CODE:
            codes.append(int(token))
        elif token not in _FILLER:
            return None
    if not codes:
        return None
    return {"source": _VERBS[tokens[0]], "codes": list(dict.fromkeys(codes)), "base": base}


def end_customer_mode(customer_id: Optional[str]) -> None:
    """El domiciliario que pedía como cliente vuelve a ser domiciliario al
    confirmar su pedido: si después reporta un problema de una entrega, lo
    tiene que atender su agente, no el de clientes (prueba real 2026-10-16)."""
    customer = Customers.collection.get(customer_id) if customer_id else None
    if customer is not None and customer.courier_as_customer_date:
        customer.courier_as_customer_date = None
        FireStoreCRUDService().update(customer)


def parse_incident_command(text: str) -> Optional[dict]:
    """"Tengo un problema con el pedido 5, no me abren" -> {"code": 5,
    "motivo": <texto>}. Exige una palabra de novedad y UN solo número (sin
    verbo de liquidación); si no, None y lo atiende el agente."""
    plain = _plain(text)
    tokens = re.sub(r"[#,;.:\-!?¿¡]", " ", plain).split()
    if not _INCIDENT_WORDS & set(tokens) or _VERBS.keys() & set(tokens):
        return None
    numbers = [int(t) for t in tokens if t.isdigit()]
    if len(numbers) != 1 or not 0 < numbers[0] <= _MAX_CODE:
        return None
    return {"code": numbers[0], "motivo": " ".join((text or "").split())}


def find_courier(configuration: Optional[Configurations], phone: str) -> Optional[dict]:
    phone = normalize_phone(phone)
    couriers = (configuration.couriers if configuration else None) or []
    return next((c for c in couriers if normalize_phone(c.get("phone")) == phone), None)


def _codes(codes: list[int]) -> str:
    return ", ".join(f"#{c}" for c in codes)


def format_summary(summary: dict, base: Optional[int] = None) -> str:
    lines = [
        f"Tu liquidación de hoy: {summary['count']} comanda(s)" + (f" ({_codes(summary['codes'])})" if summary["codes"] else ""),
        f"Transferencia: {format_cop(summary['transfer'], '-')}",
        f"Efectivo: {format_cop(summary['cash'], '-')}",
    ]
    if base is not None:
        lines.append(f"Base {format_cop(base, '-')} + efectivo = {format_cop(base + summary['cash'], '-')} en la riñonera")
    return "\n".join(lines)


def format_reply(result: dict, summary: dict, base: Optional[int], names: dict) -> str:
    lines = []
    if result["ok"]:
        lines.append(f"✅ Registradas: {_codes(result['ok'])}")
    if result["mine"]:
        lines.append(f"Ya las tenías: {_codes(result['mine'])}")
    if result["unknown"]:
        lines.append(f"❓ No existen hoy: {_codes(result['unknown'])}. Revisa el número de la comanda.")
    if result.get("own"):
        lines.append(f"🚫 {_codes(result['own'])} es un pedido tuyo: no lo puedes liquidar tú.")
    for conflict in result["conflicts"]:
        other = names.get(conflict["other"]) or local_phone(conflict["other"])
        lines.append(f"⚠️ #{conflict['code']} ya la liquidó {other}. Quedó para revisar en el cierre.")
    return "\n".join([*lines, "", format_summary(summary, base)])


async def settle_and_report(
    whatsapp_client: WhatsAppClient,
    business_id: str,
    configuration: Configurations,
    courier: dict,
    codes: list[int],
    source: str,
    base: Optional[int] = None,
) -> str:
    """Liquida `codes` a nombre del domiciliario y devuelve el texto para él
    (lo registrado, conflictos y el acumulado del día). Avisa una vez al
    otro domiciliario de cada conflicto nuevo. Lo usan la vía rápida y la
    tool del agente: las cuentas nunca las hace el modelo."""
    phone = normalize_phone(courier.get("phone"))
    result = await run_in_threadpool(tracking.settle, business_id, phone, codes, source)
    summary = await run_in_threadpool(tracking.courier_summary, business_id, phone)
    names = {normalize_phone(c.get("phone")): c.get("name") for c in configuration.couriers or []}
    for conflict in result["conflicts"]:
        if not conflict["new"]:
            continue
        # Best-effort: fuera de 24 h no le llega, pero el conflicto ya
        # quedó registrado y sale en el cierre.
        sent = await whatsapp_client.send_message(
            conflict["other"],
            f"⚠️ {courier.get('name') or local_phone(phone)} también liquidó la comanda "
            f"#{conflict['code']}, que ya era tuya. Quedó para revisar en el cierre.",
        )
        if not sent:
            logger.warning("No se pudo avisar el conflicto de la comanda #%s al otro domiciliario", conflict["code"])
    return format_reply(result, summary, base, names)


class CourierMessageHandler:
    def __init__(self, whatsapp_client: WhatsAppClient) -> None:
        self._whatsapp_client = whatsapp_client

    async def _transcribe(self, message: Messages) -> Optional[str]:
        """Texto de la nota de voz; None si no se pudo escuchar (el flush la
        reintenta y el agente pide repetirla)."""
        try:
            audio = await AudioProcessor(self._whatsapp_client).process(message.media_id, message.mime_type)
            text = await transcribe(audio)
        except AudioProcessingError as e:
            logger.warning("Audio de domiciliario sin transcribir (media_id=%s): %s", message.media_id, e)
            return None
        except Exception:
            logger.exception("Audio de domiciliario sin transcribir (media_id=%s)", message.media_id)
            return None
        return None if text == "[inaudible]" else text

    async def maybe_handle(self, business: Businesses, customer: Customers, message: Messages) -> bool:
        """True si era un comando claro de un domiciliario y ya se liquidó;
        False si no es domiciliario o el parser no lo entendió (sigue al
        agente que le toque)."""
        if message.type not in ("text", "audio"):
            return False
        business_id = business.bussiness_id
        configuration = await run_in_threadpool(TenantScopedQuery.get_one_for_business, Configurations, business_id)
        courier = find_courier(configuration, customer.phone_number)
        if courier is None:
            return False
        if message.type == "audio":
            text = await self._transcribe(message)
            if text is None:
                return False
            message.content = text
            # Guardada ya: si sigue al agente, el flush no la vuelve a transcribir.
            await run_in_threadpool(FireStoreCRUDService().update, message)
        command = parse_courier_command(message.content)
        if command is not None:
            text = await settle_and_report(
                self._whatsapp_client, business_id, configuration, courier, command["codes"], command["source"], command["base"]
            )
        else:
            # Una novedad con un número de comanda claro ("no me abren el 5")
            # se abre sin IA, esté o no en modo cliente. Si la comanda no
            # sirve (no existe, ya entregada, es suya), lo explica su agente.
            report = parse_incident_command(message.content)
            if report is None:
                return False
            result = await incidents.open_incident(business, courier, customer.customer_id, report["code"], report["motivo"])
            if not result["ok"]:
                return False
            text = result["texto"]
        await OutboundMessageService(self._whatsapp_client).send_and_persist(
            business_id=business_id,
            customer_id=customer.customer_id,
            conversation_id=message.conversation_id,
            to_phone=customer.phone_number,
            text=text,
            origin=MessageOrigin.IA.value,
        )
        return True
