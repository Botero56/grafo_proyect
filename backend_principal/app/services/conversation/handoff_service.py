"""
Pausa/reactivación del bot por conversación (handoff bot <-> humano) y
notificación al humano encargado.

`Conversations.bot_activo` es la fuente de verdad de si la IA puede
responder en esa conversación. Este servicio es el único lugar que debe
tocar ese campo (y los relacionados: motivo_handoff, pausado_desde/hasta,
agente_asignado) para que la lógica de pausa/reactivación no se disperse
entre el router del webhook, los endpoints admin y el job de reactivación.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from app.services.phones import contact_phone
from app.config import handoff_settings
from app.firestore_db.firebase_schemas import Businesses, Configurations, Conversations, Customers
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.firestore_webhook_service import wrote_within_24h
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services.time_utils import as_utc
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient


def phone_for_humans(customer) -> str:
    """El número del cliente para un aviso al equipo, o una nota si escribe
    con nombre de usuario y todavía no nos dio su número."""
    phone = contact_phone(customer)
    return local_phone(phone) if phone else "sin número (escribe con usuario de WhatsApp)"


def local_phone(number: str) -> str:
    """573001234567 -> 3001234567: como se busca en WhatsApp desde un
    celular colombiano. Cualquier otro formato sale tal cual."""
    digits = "".join(c for c in number or "" if c.isdigit())
    return digits[2:] if len(digits) == 12 and digits.startswith("57") else number


def _sentence(text: Optional[str]) -> str:
    """Una sola línea, con punto final."""
    text = " ".join((text or "").split())
    return f"{text}." if text and not text.endswith((".", "!", "?")) else text

logger = logging.getLogger("whatsapp-webhook")

# Tipos de aviso al admin con su plantilla UTILITY por negocio en
# Configurations.admin_templates (texto sugerido en docs/RUNBOOK.md).
COMANDA_RESPALDO = "comanda_respaldo"
COTIZACION_DOMICILIO = "cotizacion_domicilio"
ATENCION_CLIENTE = "atencion_cliente"
RECORDATORIO_MENU = "recordatorio_menu"
CIERRE_DIARIO = "cierre_diario"
# Un parámetro de plantilla no admite saltos de línea ni tabs, y el cuerpo
# entero tiene un tope de 1024 caracteres (el texto fijo usa el resto).
_PARAM_MAX = 900


def admin_template(configuration: Optional[Configurations], kind: str) -> Optional[tuple[str, str]]:
    """(nombre, idioma) de la plantilla de ese aviso para el negocio, o None."""
    templates = (getattr(configuration, "admin_templates", None) if configuration else None) or {}
    template = templates.get(kind) or {}
    if template.get("name"):
        return template["name"], template.get("language") or "es"
    return None


def template_param(text: str) -> str:
    """El aviso en una sola línea apta para un parámetro de plantilla."""
    flat = " · ".join(" ".join(line.split()) for line in text.splitlines() if line.strip())
    return flat if len(flat) <= _PARAM_MAX else flat[: _PARAM_MAX - 1] + "…"


def log_undelivered(business_id: str, phone: Optional[str], kind: str, reason: str) -> None:
    """Severidad ERROR: dispara la alerta admin_notice_undelivered (ops/alerts.sh)."""
    logger.error(
        "Aviso al admin NO entregado (business_id=%s, a=%s, tipo=%s): %s",
        business_id,
        f"***{phone[-4:]}" if phone else "-",
        kind,
        reason,
        extra={"fields": {"ops_event": "admin_notice_undelivered", "business_id": business_id, "notice_kind": kind}},
    )


class HandoffService:
    def __init__(self, crud: Optional[FireStoreCRUDService] = None) -> None:
        self._crud = crud or FireStoreCRUDService()

    def pause_bot(
        self,
        conversation: Conversations,
        motivo: str,
        agente_asignado: Optional[str] = None,
        pausado_hasta: Optional[datetime] = None,
        at: Optional[datetime] = None,
    ) -> Conversations:
        """`at`: momento real del evento que pausa (ej. el timestamp del
        echo según WhatsApp). Cloud Tasks no garantiza orden, así que la
        pausa se fecha por el mensaje, no por cuándo se procesó."""
        conversation.bot_activo = False
        conversation.motivo_handoff = motivo
        conversation.agente_asignado = agente_asignado
        conversation.pausado_desde = at or datetime.now(timezone.utc)
        conversation.pausado_hasta = pausado_hasta
        return self._crud.update(conversation)

    def reactivate_bot(
        self, conversation: Conversations, at: Optional[datetime] = None
    ) -> Conversations:
        """`at`: momento real del mensaje que reactiva (o ahora, si es una
        acción manual). Queda en `bot_reactivado_at` para que un echo más
        viejo que llegue tarde no vuelva a pausar."""
        conversation.bot_activo = True
        conversation.motivo_handoff = None
        conversation.agente_asignado = None
        conversation.pausado_desde = None
        conversation.pausado_hasta = None
        conversation.bot_reactivado_at = at or datetime.now(timezone.utc)
        return self._crud.update(conversation)

    def should_auto_reactivate(
        self, conversation: Conversations, now: Optional[datetime] = None
    ) -> bool:
        """True si ya pasaron HANDOFF_QUICK_REACTIVATION_SECONDS sin un
        mensaje humano (echo) en esta conversación pausada.

        Reemplaza el flujo anterior de "alguien tiene que reactivar a mano
        desde el chat admin": ahora, en cuanto el cliente vuelve a
        escribir, si la encargada ya lleva ese tiempo en silencio, el bot
        se reactiva solo y responde ese mismo mensaje con normalidad (ver
        app/services/messaging/inbound_pipeline.py::_process_customer_message). Si nunca
        hubo un echo registrado (ej. se pausó a mano por el endpoint admin
        sin que la encargada llegara a escribir), se usa `pausado_desde`
        como referencia en su lugar.
        """
        now = now or datetime.now(timezone.utc)
        reference = self.pause_reference(conversation)
        if reference is None:
            return True
        elapsed = (now - as_utc(reference)).total_seconds()
        return elapsed >= handoff_settings.HANDOFF_QUICK_REACTIVATION_SECONDS

    @staticmethod
    def pause_reference(conversation: Conversations) -> Optional[datetime]:
        """Desde cuándo cuenta la inactividad de una conversación pausada:
        el último mensaje de la encargada, o el inicio de la pausa si nunca
        escribió (ej. se pausó a mano por el endpoint admin)."""
        return conversation.ultimo_mensaje_humano_at or conversation.pausado_desde

    def get_transfer_message(self, business_id: str) -> str:
        configuration = TenantScopedQuery.get_one_for_business(Configurations, business_id)
        message = getattr(configuration, "handoff_transfer_message", None) if configuration else None
        return message or handoff_settings.HANDOFF_DEFAULT_TRANSFER_MESSAGE

    async def notify_human(
        self,
        business: Businesses,
        customer: Customers,
        motivo: str,
        detalle: Optional[str] = None,
        kind: str = ATENCION_CLIENTE,
    ) -> bool:
        """Avisa por WhatsApp al admin que una conversación necesita atención
        humana (ver notify_admins para destinatarios y ventana de 24h)."""
        text = f"🔔 {customer.name or phone_for_humans(customer)} necesita atención humana ({motivo})."
        if detalle:
            text += f" {detalle}"
        return await self.notify_admins(business, text, kind)

    async def notify_handoff(
        self,
        business: Businesses,
        customer: Customers,
        resumen: Optional[str],
        tema: Optional[str] = None,
    ) -> bool:
        """Aviso de un handoff pedido por la IA: una nota corta para que la
        encargada sepa quién es, qué pasó y para qué la buscan. El teléfono va
        como número (no como enlace) para buscarlo en el WhatsApp del negocio.

            🚨 Cliente solicita atención
            👤 Ana
            📱 3001234567
            📝 Reclamo por un pedido incompleto.
            ➡️ Necesita hablar con Doña Diana.
        """
        configuration = TenantScopedQuery.get_one_for_business(Configurations, business.bussiness_id)
        manager = (getattr(configuration, "manager_name", None) if configuration else None) or "la administradora"
        lines = ["🚨 Cliente solicita atención"]
        if customer.name and customer.name != "Cliente Nuevo":
            lines.append(f"👤 {customer.name}")
        lines.append(f"📱 {phone_for_humans(customer)}")
        lines.append(f"📝 {_sentence(resumen) or 'Pidió hablar con una persona.'}")
        tema = (tema or "").strip().rstrip(".")
        lines.append(f"➡️ Necesita hablar con {manager}" + (f" sobre {tema}." if tema else "."))
        return await self.notify_admins(business, "\n".join(lines))

    async def notify_admins(self, business: Businesses, text: str, kind: str = ATENCION_CLIENTE) -> bool:
        """Manda `text` al chat de administración del negocio.

        Destinatario: el `manager_phone` del negocio o, si no hay, sus
        `admin_phone_numbers` -- antes, sin manager_phone no se avisaba a
        nadie y sin dejar rastro (prueba real 2026-09-30). A quien lleve más
        de 24h sin escribirle al negocio se le manda la plantilla `kind` del
        negocio (Configurations.admin_templates) con el aviso como
        parámetro, porque WhatsApp no le entrega texto libre.

        Best-effort: nunca rompe el flujo. Devuelve True si al menos un aviso
        salió; cada aviso que no sale queda como ERROR
        (ops_event=admin_notice_undelivered), que dispara una alerta.
        """
        configuration = TenantScopedQuery.get_one_for_business(
            Configurations, business.bussiness_id
        )
        manager_phone = getattr(configuration, "manager_phone", None) if configuration else None
        admin_phones = (getattr(configuration, "admin_phone_numbers", None) or []) if configuration else []
        recipients = [manager_phone] if manager_phone else list(admin_phones)
        if not recipients:
            log_undelivered(business.bussiness_id, None, kind, "no hay manager_phone ni admin_phone_numbers")
            return False

        client = WhatsAppClient.for_business(business)
        now = datetime.now(timezone.utc)
        template = admin_template(configuration, kind)
        notified = False
        for phone in recipients:
            if wrote_within_24h(business.bussiness_id, phone, now):
                sent = await client.send_message(phone, text)
            elif template:
                sent = await client.send_template(phone, template[0], template[1], [template_param(text)])
            else:
                log_undelivered(business.bussiness_id, phone, kind, f"fuera de la ventana de 24h y sin plantilla '{kind}'")
                continue
            if not sent:
                log_undelivered(business.bussiness_id, phone, kind, "WhatsApp rechazó el envío")
            notified = notified or sent
        return notified
