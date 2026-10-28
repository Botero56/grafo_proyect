"""Dos barridos programados (pensados para Cloud Scheduler, mismo criterio
que app.services.conversation.reactivation_job -- no corren como loop
dentro del proceso porque Cloud Run puede reciclar la instancia):

1. `expire_pending_daily_menus`: borradores pendiente_confirmacion sin
   respuesta del admin -- recuerda una vez, luego descarta.
2. `remind_missing_daily_menus`: negocios sin menú activo para HOY cuando
   llega su `Configurations.recordatorio_menu_hora` -- le avisa al admin,
   respetando la ventana de 24h de WhatsApp (usa plantilla aprobada si ya
   pasó esa ventana). Sin hora = no se le recuerda (negocio con carta fija).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import daily_menu_settings
from app.firestore_db.firebase_schemas import (
    Businesses,
    Configurations,
    DailyMenus,
)
from app.schemas.enum_schemas import DailyMenuStatus, is_business_active
from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia
from app.services.daily_menu.daily_menu_service import DailyMenuService
from app.services.excel_report_service.constants import DEFAULT_TZ
from app.services.conversation.handoff_service import RECORDATORIO_MENU, admin_template, log_undelivered
from app.services.firestore_services.firestore_webhook_service import wrote_within_24h
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services.time_utils import as_utc
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient

logger = logging.getLogger("whatsapp-webhook")

# Plantilla j2ys_recordatorio_admin (UTILITY, es): el negocio edita solo el
# {{1}} (Configurations.recordatorio_menu_texto), sin nueva revisión de Meta.
REMINDER_MESSAGE = "Hola, te escribe tu asistente de pedidos: {}. Responde a este mensaje cuando lo tengas listo."
DEFAULT_REMINDER_TEXT = "aún no tengo el menú de hoy, envíame la foto por este chat"


def reminder_text(configuration) -> str:
    return getattr(configuration, "recordatorio_menu_texto", None) or DEFAULT_REMINDER_TEXT


async def expire_pending_daily_menus(now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    reminder_cutoff = now - timedelta(minutes=daily_menu_settings.DAILY_MENU_REMINDER_MINUTES)
    discard_cutoff = now - timedelta(minutes=daily_menu_settings.DAILY_MENU_DISCARD_MINUTES)

    # list(): .fetch() devuelve un QueryIterator de fireo (sin __len__ ni
    # __bool__, se agota al recorrerlo una vez) -- se materializa porque
    # abajo se recorre y también se le hace len().
    pending = list(
        DailyMenus.collection.filter(
            status=DailyMenuStatus.PENDIENTE_CONFIRMACION.value
        ).fetch()
    )

    service = DailyMenuService()
    reminded: list[str] = []
    discarded: list[str] = []

    for draft in pending:
        business = Businesses.collection.get(draft.business_id)
        if business is None or not draft.admin_phone or not is_business_active(business):
            continue

        if draft.reminder_sent_at is None:
            created_at = as_utc(draft.created_at) if draft.created_at else now
            if created_at <= reminder_cutoff:
                await WhatsAppClient.for_business(business).send_message(
                    draft.admin_phone,
                    "Sigo esperando que confirmes el menú de hoy que me mandaste. "
                    "Respóndeme con los botones o escríbeme los cambios que necesite.",
                )
                draft.reminder_sent_at = now
                draft.update()
                reminded.append(draft.daily_menu_id)
            continue

        if as_utc(draft.reminder_sent_at) <= discard_cutoff:
            service.cancel(draft)
            await WhatsAppClient.for_business(business).send_message(
                draft.admin_phone,
                "Descarté el menú que me mandaste porque no recibí confirmación. "
                "Mándame la foto de nuevo cuando quieras.",
            )
            discarded.append(draft.daily_menu_id)

    return {"checked": len(pending), "reminded": reminded, "discarded": discarded}


# Cada cuánto corre el job (Cloud Scheduler `*/10 * * * *`, RUNBOOK §6): a
# cada negocio se le recuerda en la corrida que cae en sus primeros 10 min.
# ponytail: sin marca de "ya recordado"; si el job falla en esa ventana, ese
# día no hay recordatorio. Guardar la marca si llega a pasar.
REMINDER_WINDOW_MINUTES = 10


def reminder_due(hora: Optional[str], now_local: datetime) -> bool:
    """True si `now_local` cae en [hora, hora + REMINDER_WINDOW_MINUTES)."""
    if not hora:
        return False
    hours, minutes = (int(x) for x in hora.split(":"))
    start = now_local.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    return start <= now_local < start + timedelta(minutes=REMINDER_WINDOW_MINUTES)


async def remind_missing_daily_menus(now: Optional[datetime] = None) -> dict:
    now_local = (now or datetime.now(DEFAULT_TZ)).astimezone(DEFAULT_TZ)
    today = resolve_fecha_vigencia(None, now_local)
    # `now` puede venir en hora local (sirve para la hora del recordatorio);
    # la ventana de 24h se mide contra ultimo_mensaje_cliente_at, en UTC.
    now_utc = datetime.now(timezone.utc)

    # list(): mismo motivo que arriba -- se recorre y se le hace len().
    businesses = list(Businesses.collection.filter(status="active").fetch())

    service = DailyMenuService()
    reminded: list[tuple[str, str]] = []

    for business in businesses:
        configuration = TenantScopedQuery.get_one_for_business(Configurations, business.bussiness_id)
        if not reminder_due(getattr(configuration, "recordatorio_menu_hora", None), now_local):
            continue
        if service.get_active_for_date(business.bussiness_id, today) is not None:
            continue
        # El admin ya avisó que hoy no abren (mark_closed_today, tool del
        # admin_agent) -- no tiene sentido seguir pidiéndole la foto.
        if service.get_closed_for_date(business.bussiness_id, today) is not None:
            continue

        admin_phones = (configuration.admin_phone_numbers or []) if configuration else []
        if not admin_phones:
            continue

        client = WhatsAppClient.for_business(business)
        param = reminder_text(configuration)
        template = admin_template(configuration, RECORDATORIO_MENU)

        for admin_phone in admin_phones:
            if wrote_within_24h(business.bussiness_id, admin_phone, now_utc):
                sent = await client.send_message(admin_phone, REMINDER_MESSAGE.format(param))
            elif template:
                sent = await client.send_template(admin_phone, *template, [param])
            else:
                log_undelivered(
                    business.bussiness_id, admin_phone, RECORDATORIO_MENU, "fuera de la ventana de 24h y sin plantilla"
                )
                continue
            if sent:
                reminded.append((business.bussiness_id, admin_phone))
            else:
                log_undelivered(business.bussiness_id, admin_phone, RECORDATORIO_MENU, "WhatsApp rechazó el envío")

    return {"checked": len(businesses), "reminded": reminded}
