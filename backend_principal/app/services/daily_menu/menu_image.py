"""Envío de la foto del menú vigente al cliente, con registro en la
conversación para no repetirla.

La usan el worker (primer turno del día, sin depender del modelo: ver
inbound_pipeline.flush_conversation) y la tool send_menu_image (cuando el
cliente la pide de nuevo)."""

import logging

from starlette.concurrency import run_in_threadpool

from app.firestore_db.firebase_schemas import Businesses, Configurations, Conversations, Customers
from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia
from app.services.daily_menu.daily_menu_service import DailyMenuService
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient

logger = logging.getLogger("whatsapp-webhook")


def _auto_send_enabled(business_id: str) -> bool:
    configuration = TenantScopedQuery.get_one_for_business(Configurations, business_id)
    return configuration is None or configuration.enviar_menu_al_iniciar is not False


async def send_menu_image(
    whatsapp_client: WhatsAppClient,
    business: Businesses,
    customer: Customers,
    conversation: Conversations,
    *,
    auto: bool,
) -> bool:
    """True si la foto salió. Con `auto=True` solo la manda si el negocio
    lo tiene activado y el cliente todavía no tiene esta versión (primer
    turno del día, o el admin publicó otra)."""
    fecha = resolve_fecha_vigencia(None)
    image = await run_in_threadpool(DailyMenuService().current_menu_image, business.bussiness_id, fecha)
    if image is None:
        return False
    version, media_ids, link = image
    key = f"{fecha}:{version}"
    if auto and (conversation.menu_enviado == key or not await run_in_threadpool(_auto_send_enabled, business.bussiness_id)):
        return False

    # Capturas del catálogo: se manda el link, no las capturas. Una carta
    # en foto puede venir en varias: van todas, en orden.
    sent = bool(link) and await whatsapp_client.send_message(
        customer.phone_number, f"Aquí puedes ver todos nuestros productos: {link}"
    )
    for media_id in media_ids:
        sent = await whatsapp_client.send_image(customer.phone_number, media_id) or sent
    if sent:
        conversation.menu_enviado = key
        await run_in_threadpool(FireStoreCRUDService().update, conversation)
        logger.info("Imagen del menú enviada", extra={"fields": {"menu_version": key, "auto": auto}})
    return sent
