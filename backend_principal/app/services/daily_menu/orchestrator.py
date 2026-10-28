"""Punto de entrada de la carga del menú por foto: intercepta mensajes del
admin ANTES de que lleguen al admin_agent conversacional.

Un solo flujo para todo lo que el admin sube (menú del día y/o carta, en
una o varias fotos):
  - cada foto se lee y queda pendiente (el menú del día en su borrador
    pendiente_confirmacion; los productos de carta en el borrador
    carta_pendiente), y se le muestra al admin lo que se leyó;
  - después de cada foto llegan los mismos botones:
    subida:reemplazar (hoy queda solo lo de estas fotos),
    subida:agregar (se suma a lo que ya hay hoy) o subida:cancelar;
  - mientras hay un menú del día pendiente, un texto se toma como una
    corrección de ese menú.
El menú del día y la carta conviven: uno no borra al otro.

También atiende la pregunta "¿catálogo o menú en foto?" (cartatipo:...), el
link del catálogo y el clic Sí/No de un cambio de stock (stock:..., ver
stock.py).

Si ninguno de estos casos aplica, `maybe_handle` devuelve False y el
webhook sigue el flujo normal hacia admin_agent (ver
app/services/messaging/inbound_pipeline.py). Solo se invoca para mensajes de un admin ya
autorizado -- la verificación de rol vive en el webhook, no acá.
"""

import asyncio
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Optional

from starlette.concurrency import run_in_threadpool

from app.firestore_db.firebase_schemas import Businesses, Configurations, Customers, DailyMenus, Messages
from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia
from app.services.daily_menu import stock
from app.services.daily_menu.catalog_sync import CARTA_PREFIX, apply_catalog
from app.services.daily_menu.correction_extractor import MenuCorrectionError, apply_correction
from app.services.daily_menu.daily_menu_service import DailyMenuService
from app.services.daily_menu.media_downloader import MenuImageDownloadError, MenuImageDownloader
from app.services.daily_menu.menu_diff import diff_menus, format_cop
from app.services.daily_menu.menu_formatter import (
    format_confirmation_message,
    upload_buttons,
    upload_question,
)
from app.services.daily_menu.vision_extractor import (
    ExtractedMenu,
    MenuExtractionError,
    extract_upload_from_image,
)
from app.services.excel_report_service.constants import WEEKDAY_NAMES_ES
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services.time_utils import as_utc
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient

logger = logging.getLogger("whatsapp-webhook")

_IMAGE_CAPTION_PLACEHOLDERS = (None, "", "[Imagen recibida]")
_URL = re.compile(r"https?://\S+")
# Botones de antes del flujo único (menu:{id}:confirm, carta:{id}:juntar...)
# que pueden quedar en el chat del admin: se traducen al flujo actual.
_LEGACY_ACTIONS = {
    "confirm": "agregar",
    "juntar": "agregar",
    "nuevo": "reemplazar",
    "cancel": "cancelar",
    "cancelar": "cancelar",
}

# Serializa las operaciones de menú por negocio: MenuIngestionOrchestrator
# se instancia una vez por request (no es un singleton como
# AgentRunnerService), así que el lock vive a nivel de módulo -- si no,
# dos imágenes casi simultáneas (o una imagen y un click de botón) podrían
# pisarse: ambas verían "no hay pendiente" antes de que la otra terminara
# de crear su borrador, y quedarían dos pendiente_confirmacion a la vez
# (bug real detectado en revisión de código, 2026-09-22). Mismo patrón que
# AgentRunnerService._locks para conversaciones.
_business_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
# Fotos de cada negocio recibidas y todavía sin leer (incluida la que se lee
# ahora). Con varias fotos seguidas los botones salen una sola vez, después
# de la última: antes salían tras cada foto, el admin los apretaba todos y
# cada clic quedaba en fila detrás de las fotos (prueba real 2026-10-21).
# ponytail: en memoria, vale porque el worker corre con --max-instances 1.
_images_in_flight: defaultdict[str, int] = defaultdict(int)
# Cuándo se resolvió la última subida de cada negocio: los clics que ya
# estaban en fila en ese momento no repiten "no hay nada pendiente" uno por uno.
_last_resolved: dict[str, datetime] = {}


class MenuIngestionOrchestrator:
    def __init__(
        self, whatsapp_client: WhatsAppClient, service: Optional[DailyMenuService] = None
    ) -> None:
        self._whatsapp_client = whatsapp_client
        self._service = service or DailyMenuService()

    async def maybe_handle(
        self, business: Businesses, customer: Customers, message: Messages
    ) -> bool:
        business_id = business.bussiness_id
        if message.type != "image":
            async with _business_locks[business_id]:
                return await self._maybe_handle_locked(business, customer, message)

        _images_in_flight[business_id] += 1
        try:
            async with _business_locks[business_id]:
                await self._handle_image(business, customer, message)
                if _images_in_flight[business_id] == 1:  # era la última de la tanda
                    await self._ask_upload_decision(business_id, customer.phone_number)
        finally:
            _images_in_flight[business_id] -= 1
        return True

    async def _maybe_handle_locked(
        self, business: Businesses, customer: Customers, message: Messages
    ) -> bool:
        button_id = (message.content or "") if message.type == "interactive" else ""
        if button_id.startswith("subida:"):
            await self._apply_upload(business, customer, button_id.split(":")[-1], message.created_at)
            return True
        if button_id.startswith(("menu:", "carta:")):
            await self._handle_legacy_button(business, customer, button_id)
            return True
        if button_id.startswith("cartatipo:"):
            await self._handle_carta_tipo(business, customer, button_id)
            return True
        if button_id.startswith(stock.BUTTON_PREFIX):
            reply = await run_in_threadpool(stock.apply_button, business.bussiness_id, button_id)
            await self._whatsapp_client.send_message(customer.phone_number, reply)
            return True
        if message.type == "text" and await self._maybe_save_catalog_link(business, customer, message.content or ""):
            return True

        pending = await run_in_threadpool(self._service.get_pending, business.bussiness_id)
        if pending is not None and message.type == "text" and message.content:
            await self._handle_correction(customer, pending, message.content)
            return True

        return False

    # --- Fotos ---

    async def _handle_image(self, business: Businesses, customer: Customers, message: Messages) -> None:
        await self._whatsapp_client.send_message(
            customer.phone_number, "Recibí la foto, la estoy leyendo…"
        )

        if not message.media_id:
            logger.error("Mensaje de imagen sin media_id (message_id=%s)", message.message_id)
            return

        try:
            downloaded = await MenuImageDownloader(self._whatsapp_client).download(message.media_id)
        except MenuImageDownloadError:
            logger.exception(
                "No se pudo descargar la imagen del menú (media_id=%s)", message.media_id
            )
            await self._whatsapp_client.send_message(
                customer.phone_number,
                "No pude descargar la imagen del menú. ¿Puedes volver a mandarla?",
            )
            return

        caption = None if message.content in _IMAGE_CAPTION_PLACEHOLDERS else message.content
        fecha_vigencia = resolve_fecha_vigencia(caption)

        previous_menu = await self._last_active_menu(business.bussiness_id)

        # Una sola lectura por foto: trae el menú del día y la carta a la vez.
        # Es una llamada bloqueante a Gemini (varios segundos): fuera del
        # threadpool, congelaba el event loop entero y con eso CUALQUIER otro
        # webhook en curso durante ese tiempo -- bug real detectado en
        # revisión de código, 2026-09-22.
        try:
            upload = await run_in_threadpool(
                extract_upload_from_image,
                downloaded.data,
                downloaded.mime_type,
                caption,
                previous_menu,
            )
        except MenuExtractionError:
            logger.exception("Extracción de menú falló (media_id=%s)", message.media_id)
            await self._whatsapp_client.send_message(
                customer.phone_number,
                "No pude leer la imagen. ¿Puedes mandar una foto más clara?",
            )
            return

        has_daily = upload.menu.parece_menu()
        if not has_daily and not upload.productos:
            await self._whatsapp_client.send_message(
                customer.phone_number,
                "Esa imagen no parece ser un menú ni una carta con precios. Si sí lo es, "
                "mándame una foto más clara.",
            )
            return

        media_id = await self._whatsapp_client.upload_media(
            downloaded.data, "menu.jpg", downloaded.mime_type
        )
        if has_daily:
            extracted = upload.menu
            _maybe_add_date_mismatch_warning(extracted, fecha_vigencia)
            # Un día tiene un solo menú del día: otra foto de menú del día
            # reemplaza al pendiente (create_draft descarta el anterior).
            draft = await run_in_threadpool(
                self._service.create_draft,
                business_id=business.bussiness_id,
                admin_phone=customer.phone_number,
                fecha_vigencia=fecha_vigencia,
                extracted_menu=extracted,
                source_media_id=message.media_id,
                source_image_path=str(downloaded.file_path),
                source_sha256=downloaded.sha256,
                caption=caption,
            )
            draft.menu_media_id = media_id
            await run_in_threadpool(FireStoreCRUDService().update, draft)
            await self._send_menu_text(customer.phone_number, draft, extracted)

        # Toda foto queda en la subida de hoy (aunque sea solo menú del día):
        # ahí viven los productos de carta, las fotos que se le mandan al
        # cliente y si es catálogo o menú en foto. La foto del menú del día
        # ya va en su borrador: no se repite.
        await run_in_threadpool(
            self._service.add_carta_page,
            business.bussiness_id,
            customer.phone_number,
            resolve_fecha_vigencia(None),
            [p.model_dump() for p in upload.productos],
            None if has_daily else media_id,
        )
        if upload.productos:
            leidos = ", ".join(f"{p.nombre} {format_cop(p.precio, 'sin precio')}" for p in upload.productos)
            await self._whatsapp_client.send_message(
                customer.phone_number, f"*Carta* ({len(upload.productos)} productos): {leidos}"
            )

    async def _ask_upload_decision(self, business_id: str, admin_phone: str) -> None:
        """Los mismos botones después de cada foto o corrección, con un
        resumen de todo lo que está pendiente por guardar."""
        daily = await run_in_threadpool(self._service.get_pending, business_id)
        carta = await run_in_threadpool(
            self._service.get_carta_draft, business_id, resolve_fecha_vigencia(None)
        )
        if daily is None and carta is None:
            return  # la foto no se pudo leer y no hay nada pendiente
        data = carta.extracted_menu if carta is not None else {}
        await self._whatsapp_client.send_interactive_buttons(
            admin_phone,
            upload_question(
                daily.fecha_vigencia if daily is not None else None,
                len(data.get("productos", [])),
                len(data.get("media_ids", [])),
            ),
            upload_buttons(),
        )

    # --- Guardar ---

    async def _apply_upload(
        self, business: Businesses, customer: Customers, action: str, clicked_at: Optional[datetime] = None
    ) -> None:
        """Guarda (o descarta) TODO lo pendiente: el menú del día y la carta.

        reemplazar: hoy queda solo lo de estas fotos (si no trajeron menú del
        día, se quita el de hoy; si no trajeron carta, se quita la carta).
        agregar: el menú del día de las fotos reemplaza al de hoy (hay uno
        por día) y los productos de carta se suman a los que ya había."""
        business_id = business.bussiness_id
        phone = customer.phone_number
        today = resolve_fecha_vigencia(None)
        daily = await run_in_threadpool(self._service.get_pending, business_id)
        carta = await run_in_threadpool(self._service.get_carta_draft, business_id, today)
        if action not in ("reemplazar", "agregar", "cancelar"):
            return
        if daily is None and carta is None:
            resolved = _last_resolved.get(business_id)
            if clicked_at is not None and resolved is not None and as_utc(clicked_at) <= resolved:
                return  # clic que esperaba en fila detrás del que ya guardó todo
            await self._whatsapp_client.send_message(
                phone, "No hay nada pendiente por guardar (puede que ya se haya resuelto). "
                "Si quieres, mándame las fotos de nuevo.",
            )
            return
        _last_resolved[business_id] = datetime.now(timezone.utc)
        if action == "cancelar":
            for draft in (daily, carta):
                if draft is not None:
                    await run_in_threadpool(self._service.cancel, draft)
            await self._whatsapp_client.send_message(phone, "Listo, descarté esas fotos.")
            return

        replace = action == "reemplazar"
        note = ""
        if daily is not None:
            note = await self._confirm_daily(daily)
        elif replace:
            await run_in_threadpool(self._service.retire_today, business_id, today)
        if carta is not None:
            await self._activate_carta(business_id, carta, merge=not replace)
        elif replace:
            await self._retire_carta(business_id)

        texts = await run_in_threadpool(self._service.today_menu_texts, business_id, today)
        header = "*Listo, así quedó el menú de hoy para tus clientes*" + note
        texts = texts or ["Hoy no quedó ni menú del día ni carta."]
        texts[0] = f"{header}\n\n{texts[0]}"
        for text in texts:
            await self._whatsapp_client.send_message(phone, text)

        if carta is not None:
            await self._whatsapp_client.send_interactive_buttons(
                phone,
                "¿Estas fotos son capturas de tu catálogo de WhatsApp o la foto de tu menú? "
                "Si es catálogo, a los clientes les mando el link en vez de las fotos.",
                [
                    {"id": f"cartatipo:{carta.daily_menu_id}:catalogo", "title": "📒 Catálogo"},
                    {"id": f"cartatipo:{carta.daily_menu_id}:foto", "title": "🖼️ Menú en foto"},
                ],
            )

    async def _handle_legacy_button(self, business: Businesses, customer: Customers, button_id: str) -> None:
        """Botones con el id del borrador (menu:{id}:accion, carta:{id}:accion):
        un clic en uno que ya no está pendiente no toca nada."""
        parts = button_id.split(":")
        pending = [
            await run_in_threadpool(self._service.get_pending, business.bussiness_id),
            await run_in_threadpool(
                self._service.get_carta_draft, business.bussiness_id, resolve_fecha_vigencia(None)
            ),
        ]
        if len(parts) != 3 or parts[1] not in {d.daily_menu_id for d in pending if d is not None}:
            await self._whatsapp_client.send_message(
                customer.phone_number,
                "Ese menú ya no está pendiente (puede que ya se haya resuelto). "
                "Si quieres, mándame la foto de nuevo.",
            )
            return
        if parts[2] == "correct":
            await self._whatsapp_client.send_message(
                customer.phone_number,
                'Cuéntame qué cambios hago (ej. "el almuerzo vale 18.000", "quita la sopa").',
            )
            return
        await self._apply_upload(business, customer, _LEGACY_ACTIONS.get(parts[2], ""))

    async def _confirm_daily(self, draft: DailyMenus) -> str:
        """Deja activo el menú del día pendiente. Devuelve una nota para el
        admin si hubo que corregir la fecha."""
        # La fecha de vigencia se calculó al SUBIR la foto -- si el admin
        # confirma justo pasada la medianoche, "hoy" ya cambió y el menú
        # quedaría guardado para el día equivocado (edge case real detectado
        # en revisión de código, 2026-09-22). Se recalcula con el mismo
        # caption justo antes de confirmar y, si cambió, se corrige y se
        # avisa -- nunca se guarda callado para una fecha que ya pasó.
        note = ""
        fresh_fecha_vigencia = resolve_fecha_vigencia(draft.caption)
        if fresh_fecha_vigencia != draft.fecha_vigencia:
            note = (
                f" (¡ojo! ya cambió el día desde que mandaste la foto -- lo guardé "
                f"para hoy, {fresh_fecha_vigencia}, no para {draft.fecha_vigencia})"
            )
            draft.fecha_vigencia = fresh_fecha_vigencia
        await run_in_threadpool(self._service.confirm, draft)
        return note

    async def _activate_carta(self, business_id: str, draft: DailyMenus, merge: bool) -> None:
        await run_in_threadpool(
            apply_catalog,
            business_id,
            [
                {
                    "retailer_id": f"{CARTA_PREFIX}{p['categoria']}:{p['nombre']}".lower(),
                    "name": p["nombre"],
                    "description": p.get("descripcion"),
                    # str(int()): parse_price trata ".0" final como decimal mal formado.
                    "price": str(int(p["precio"])) if p.get("precio") is not None else None,
                    "categoria": p["categoria"],
                }
                for p in draft.extracted_menu["productos"]
            ],
            True,
            not merge,
        )
        media_ids = await run_in_threadpool(self._service.activate_carta, draft, merge)
        await self._save_carta_image(business_id, media_ids[0] if media_ids else None)

    async def _retire_carta(self, business_id: str) -> None:
        """Quita la carta (productos y fotos): "reemplazar todo" con fotos
        que no traían carta."""
        await run_in_threadpool(apply_catalog, business_id, [], True)
        await run_in_threadpool(self._service.retire_carta, business_id)
        configuration = await run_in_threadpool(
            TenantScopedQuery.get_one_for_business, Configurations, business_id
        )
        if configuration is not None:
            configuration.carta_media_id = None
            await run_in_threadpool(FireStoreCRUDService().update, configuration)

    async def _save_carta_image(self, business_id: str, media_id: Optional[str]) -> None:
        """Guarda la primera foto de la carta activa en carta_media_id (None
        si no tiene). Las fotos que se mandan salen de la carta activa (ver
        DailyMenuService.current_menu_image); este campo es para las cartas
        de antes de las subidas."""
        configuration = await run_in_threadpool(
            TenantScopedQuery.get_one_for_business, Configurations, business_id
        )
        if configuration is None or configuration.carta_media_id == media_id:
            return
        configuration.carta_media_id = media_id
        await run_in_threadpool(FireStoreCRUDService().update, configuration)

    # --- Catálogo o menú en foto ---

    async def _handle_carta_tipo(self, business: Businesses, customer: Customers, button_id: str) -> None:
        """Respuesta a "¿catálogo o menú en foto?": define qué se le manda al
        cliente (el link del catálogo o las fotos)."""
        _, carta_id, tipo = (button_id.split(":") + ["", ""])[:3]
        active = await run_in_threadpool(self._service.get_active_carta, business.bussiness_id)
        if active is None or active.daily_menu_id != carta_id or tipo not in ("catalogo", "foto"):
            await self._whatsapp_client.send_message(
                customer.phone_number, "Esa carta ya no es la vigente, no cambié nada."
            )
            return
        if tipo == "foto":
            await run_in_threadpool(
                self._service.update_active_carta, business.bussiness_id, tipo="foto", esperando_link=False
            )
            await self._whatsapp_client.send_message(
                customer.phone_number, "Listo: a los clientes que pidan el menú les mando estas fotos."
            )
            return
        await run_in_threadpool(
            self._service.update_active_carta, business.bussiness_id, tipo="catalogo", esperando_link=True
        )
        link = (active.extracted_menu or {}).get("link")
        await self._whatsapp_client.send_message(
            customer.phone_number,
            f"Listo. Sigo mandando el link de antes: {link}\nSi cambió, mándame el nuevo."
            if link
            else "Listo. Mándame el link de tu catálogo (en WhatsApp Business: Herramientas > "
            "Catálogo > compartir) y se lo envío a los clientes en vez de las capturas.",
        )

    async def _maybe_save_catalog_link(self, business: Businesses, customer: Customers, text: str) -> bool:
        """Si se está esperando el link del catálogo y el admin lo manda, se
        guarda. False para cualquier otro texto (sigue a la IA)."""
        found = _URL.search(text)
        if found is None:
            return False
        active = await run_in_threadpool(self._service.get_active_carta, business.bussiness_id)
        if active is None or not (active.extracted_menu or {}).get("esperando_link"):
            return False
        link = found.group(0).rstrip(".,;)")
        await run_in_threadpool(
            self._service.update_active_carta, business.bussiness_id, link=link, esperando_link=False
        )
        await self._whatsapp_client.send_message(
            customer.phone_number,
            f"Guardado. A los clientes que pidan el menú les mando este link: {link}",
        )
        return True

    # --- Correcciones del menú del día ---

    async def _handle_correction(self, customer: Customers, draft: DailyMenus, text: str) -> None:
        current_menu = self._service.get_extracted_menu(draft)
        try:
            # apply_correction también es una llamada bloqueante a Gemini,
            # mismo motivo que extract_upload_from_image arriba.
            updated = await run_in_threadpool(apply_correction, current_menu, text)
        except MenuCorrectionError:
            logger.exception("No se pudo aplicar la corrección al menú %s", draft.daily_menu_id)
            await self._whatsapp_client.send_message(
                customer.phone_number,
                "No pude aplicar ese cambio, ¿puedes escribirlo de otra forma?",
            )
            return

        await run_in_threadpool(self._service.apply_correction, draft, text, updated)
        await self._send_menu_text(customer.phone_number, draft, updated)
        await self._ask_upload_decision(draft.business_id, customer.phone_number)

    async def _last_active_menu(self, business_id: str) -> Optional[ExtractedMenu]:
        """El último menú confirmado del negocio (referencia de formato y
        base del diff), o None si nunca confirmó uno."""
        previous_active = await run_in_threadpool(self._service.get_last_active, business_id)
        return self._service.get_extracted_menu(previous_active) if previous_active is not None else None

    async def _send_menu_text(self, admin_phone: str, draft: DailyMenus, extracted: ExtractedMenu) -> None:
        # Siempre se compara contra el último menú ACTIVO del negocio (no
        # contra el estado anterior del borrador), tanto en la carga
        # inicial como después de cada corrección -- es la misma
        # referencia en todo el ciclo de confirmación.
        previous_menu = await self._last_active_menu(draft.business_id)
        changes = diff_menus(previous_menu, extracted)

        for text in format_confirmation_message(extracted, draft.fecha_vigencia, changes):
            await self._whatsapp_client.send_message(admin_phone, text)


def _maybe_add_date_mismatch_warning(extracted: ExtractedMenu, fecha_vigencia: str) -> None:
    detected = (extracted.fecha_detectada or "").upper()
    if not detected:
        return

    vigencia_date = date.fromisoformat(fecha_vigencia)
    vigencia_weekday = WEEKDAY_NAMES_ES[vigencia_date.weekday()].upper()

    for weekday_name in WEEKDAY_NAMES_ES:
        upper_name = weekday_name.upper()
        if upper_name in detected and upper_name != vigencia_weekday:
            extracted.advertencias.append(
                f"La imagen dice {upper_name} pero el menú se está guardando para "
                f"{vigencia_weekday} ({fecha_vigencia})."
            )
            break
