"""Ciclo de vida de un DailyMenu: crear borrador, descartar el pendiente
anterior, confirmar (activo/reemplazado), cancelar, aplicar corrección.

Es el análogo de app.services.conversation.handoff_service.HandoffService
para el menú del día: el único lugar que debe tocar `DailyMenus.status` y
campos relacionados.
"""

from datetime import datetime, timezone
from typing import Optional

from app.firestore_db.firebase_schemas import Configurations, DailyMenus, FixedMenuItems
from app.schemas.enum_schemas import DailyMenuStatus
from app.services import opening_hours
from app.services.daily_menu.menu_formatter import format_carta_text, format_confirmation_message
from app.services.daily_menu.vision_extractor import ExtractedMenu
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery

# Versión actual del esquema de extracted_menu (ver vision_extractor.py:
# ExtractedMenu -- menu_del_dia/acompanamientos/otras_opciones). Un solo
# formato soportado: no hay lectura condicional por versión en ningún lado,
# esto es solo el sello que se guarda en cada borrador nuevo.
# v2 -> v3 (2026-09-23): el campo "plato_del_dia" se renombró a
# "menu_del_dia" para que el nombre del campo sea la señal semántica de que
# eso es "el menú del día" que preguntan los clientes -- ExtractedMenu sigue
# aceptando la clave vieja al leer (validation_alias), así que documentos ya
# guardados con v2 se siguen leyendo sin migración.
SCHEMA_VERSION = 3


class DailyMenuService:
    def __init__(self, crud: Optional[FireStoreCRUDService] = None) -> None:
        self._crud = crud or FireStoreCRUDService()

    def get_pending(self, business_id: str) -> Optional[DailyMenus]:
        return DailyMenus.collection.filter(
            business_id=business_id, status=DailyMenuStatus.PENDIENTE_CONFIRMACION.value
        ).get()

    def get_active_for_date(self, business_id: str, fecha_vigencia: str) -> Optional[DailyMenus]:
        return DailyMenus.collection.filter(
            business_id=business_id,
            fecha_vigencia=fecha_vigencia,
            status=DailyMenuStatus.ACTIVO.value,
        ).get()

    def get_closed_for_date(self, business_id: str, fecha_vigencia: str) -> Optional[DailyMenus]:
        """El registro CERRADO de ese día, si el admin avisó que no abren
        (ver mark_closed_today). None si no hay ninguno -- no implica nada
        sobre si el negocio abre o no, solo que nadie lo marcó como cerrado."""
        return DailyMenus.collection.filter(
            business_id=business_id,
            fecha_vigencia=fecha_vigencia,
            status=DailyMenuStatus.CERRADO.value,
        ).get()

    def get_last_active(self, business_id: str) -> Optional[DailyMenus]:
        """Último menú ACTIVO del negocio, sin importar la fecha -- se usa
        como referencia de formato para la extracción y como base del diff
        de cambios (ver vision_extractor.extract_upload_from_image y
        menu_diff.py). No necesariamente es "el de ayer": si el admin no
        confirmó nada un día, sigue siendo el último que sí confirmó."""
        # .fetch() devuelve un QueryIterator de fireo, no una lista: no
        # implementa __bool__/__len__ (bool(...) da siempre True, y se
        # agota al recorrerlo una vez), así que hay que materializarlo con
        # list() ANTES de chequear si está vacío o de pasarlo a max()/len()
        # -- si no, "not candidates" nunca es True y max() revienta con
        # ValueError sobre un iterador ya vacío (bug real visto en
        # producción: 2026-09-21, ver también reactivation_job.py y
        # reminders.py que tenían el mismo problema).
        candidates = list(
            DailyMenus.collection.filter(
                business_id=business_id, status=DailyMenuStatus.ACTIVO.value
            ).fetch()
        )
        if not candidates:
            return None
        return max(candidates, key=lambda d: d.fecha_vigencia or "")

    def current_menu_image(
        self, business_id: str, fecha_vigencia: str
    ) -> Optional[tuple[str, list[str], Optional[str]]]:
        """(versión, [media_id, ...], link del catálogo) de lo que debe ver
        hoy el cliente, o None.

        Si el admin dijo que sus fotos son capturas del catálogo: solo el
        link. Si no: la foto del menú del día de hoy (nunca la de otro día)
        más las fotos de la carta activa."""
        if self.get_closed_for_date(business_id, fecha_vigencia) is not None:
            return None
        active = self.get_active_for_date(business_id, fecha_vigencia)
        record = self.get_active_carta(business_id)
        data = (record.extracted_menu or {}) if record is not None else {}
        if data.get("tipo") == "catalogo":
            # Capturas del catálogo: solo el link, ni las capturas ni la foto
            # del menú del día.
            return (record.daily_menu_id, [], data["link"]) if data.get("link") else None

        versions, media_ids = [], []
        if active is not None and active.menu_media_id:
            versions.append(active.daily_menu_id)
            media_ids.append(active.menu_media_id)
        if record is not None and data.get("media_ids"):
            versions.append(record.daily_menu_id)
            media_ids += data["media_ids"]
        elif record is None and active is None:
            legacy = self._legacy_carta_image(business_id)
            if legacy:
                return legacy, [legacy], None
        return (":".join(versions), media_ids, None) if media_ids else None

    def _legacy_carta_image(self, business_id: str) -> Optional[str]:
        """La foto de una carta de antes de las subidas (carta_media_id sin
        carta activa). Un carta_media_id igual a la foto del último menú del
        día es un resto de cuando ahí se guardaba también esa foto: se ignora."""
        configuration = TenantScopedQuery.get_one_for_business(Configurations, business_id)
        carta = configuration.carta_media_id if configuration else None
        last = self.get_last_active(business_id)
        if not carta or (last is not None and last.menu_media_id == carta):
            return None
        return carta

    def today_menu_texts(self, business_id: str, fecha_vigencia: str) -> list[str]:
        """El menú de hoy como lo ven los clientes: el menú del día y la
        carta (productos fijos). Vacío si no hay ninguno."""
        active = self.get_active_for_date(business_id, fecha_vigencia)
        texts = format_confirmation_message(self.get_extracted_menu(active), fecha_vigencia) if active else []
        items = self.active_fixed_items(business_id)
        if items:
            carta = format_carta_text(items)
            carta[0] = f"*Carta*\n\n{carta[0]}"
            texts += carta
        return texts

    # --- Carta en varias fotos ---

    def get_active_carta(self, business_id: str) -> Optional[DailyMenus]:
        return DailyMenus.collection.filter(
            business_id=business_id, status=DailyMenuStatus.CARTA_ACTIVA.value
        ).get()

    def get_carta_draft(self, business_id: str, fecha_vigencia: str) -> Optional[DailyMenus]:
        """El borrador de carta que se está armando hoy. Uno de otro día se
        descarta: el admin lo dejó a medias."""
        draft = DailyMenus.collection.filter(
            business_id=business_id, status=DailyMenuStatus.CARTA_PENDIENTE.value
        ).get()
        if draft is not None and draft.fecha_vigencia != fecha_vigencia:
            self.cancel(draft)
            return None
        return draft

    def add_carta_page(
        self,
        business_id: str,
        admin_phone: str,
        fecha_vigencia: str,
        productos: list[dict],
        media_id: Optional[str],
    ) -> DailyMenus:
        """Suma una foto (sus productos y su media_id) al borrador de hoy,
        creándolo si no existe."""
        draft = self.get_carta_draft(business_id, fecha_vigencia)
        if draft is None:
            draft = DailyMenus()
            draft.business_id = business_id
            draft.admin_phone = admin_phone
            draft.schema_version = SCHEMA_VERSION
            draft.fecha_vigencia = fecha_vigencia
            draft.status = DailyMenuStatus.CARTA_PENDIENTE.value
            draft.extracted_menu = {"productos": [], "media_ids": []}
            draft.correction_history = []
            draft.created_at = datetime.now(timezone.utc)
        data = draft.extracted_menu
        draft.extracted_menu = {
            "productos": data["productos"] + productos,
            "media_ids": data["media_ids"] + ([media_id] if media_id else []),
        }
        draft.save()
        return draft

    def retire_carta(self, business_id: str) -> None:
        """La carta activa deja de serlo (un menú del día la reemplazó): un
        "juntar" posterior ya no le suma sus fotos."""
        active = self.get_active_carta(business_id)
        if active is not None:
            active.status = DailyMenuStatus.REEMPLAZADO.value
            self._crud.update(active)

    def activate_carta(self, draft: DailyMenus, merge: bool) -> list[str]:
        """Deja el borrador como la carta activa y devuelve sus fotos. Con
        merge, se le suman al principio las fotos de la carta anterior. El
        link del catálogo de la anterior se conserva para volver a usarlo."""
        previous = self.get_active_carta(draft.business_id)
        media_ids = list(draft.extracted_menu["media_ids"])
        link = None
        if previous is not None:
            if merge:
                media_ids = list((previous.extracted_menu or {}).get("media_ids", [])) + media_ids
            link = (previous.extracted_menu or {}).get("link")
            previous.status = DailyMenuStatus.REEMPLAZADO.value
            self._crud.update(previous)
        draft.extracted_menu = {**draft.extracted_menu, "media_ids": media_ids, "link": link}
        draft.status = DailyMenuStatus.CARTA_ACTIVA.value
        draft.confirmed_at = datetime.now(timezone.utc)
        self._crud.update(draft)
        self._reopen(draft.business_id, draft.fecha_vigencia)
        return media_ids

    def _reopen(self, business_id: str, fecha_vigencia: str) -> None:
        """Gana lo último: subir menú o carta anula el "hoy no abrimos" de
        ese día (si no, todas las lecturas miran primero el cerrado)."""
        closed = self.get_closed_for_date(business_id, fecha_vigencia)
        if closed is not None:
            closed.status = DailyMenuStatus.REEMPLAZADO.value
            self._crud.update(closed)
            opening_hours.invalidate(business_id)

    def update_active_carta(self, business_id: str, **fields) -> Optional[DailyMenus]:
        """Cambia campos de la carta activa (tipo "catalogo"|"foto", link,
        esperando_link). None si no hay carta activa."""
        active = self.get_active_carta(business_id)
        if active is not None:
            active.extracted_menu = {**(active.extracted_menu or {}), **fields}
            self._crud.update(active)
        return active

    def active_fixed_items(self, business_id: str) -> list[FixedMenuItems]:
        """Los productos fijos disponibles (carta, catálogo y los creados a mano)."""
        # list(): .fetch() de fireo devuelve un QueryIterator, no una lista.
        return list(FixedMenuItems.collection.filter(business_id=business_id, is_active=True).fetch())

    def discard_pending_if_any(self, business_id: str) -> None:
        pending = self.get_pending(business_id)
        if pending is not None:
            self.cancel(pending)

    def _replace_active(
        self, business_id: str, fecha_vigencia: str, keep_id: Optional[str] = None
    ) -> None:
        """Marca REEMPLAZADO el menú activo de esa fecha (salvo `keep_id`):
        un día tiene a lo sumo un menú activo."""
        previous_active = self.get_active_for_date(business_id, fecha_vigencia)
        if previous_active is not None and previous_active.daily_menu_id != keep_id:
            previous_active.status = DailyMenuStatus.REEMPLAZADO.value
            self._crud.update(previous_active)

    def create_draft(
        self,
        business_id: str,
        admin_phone: str,
        fecha_vigencia: str,
        extracted_menu: ExtractedMenu,
        source_media_id: str,
        source_image_path: str,
        source_sha256: str,
        caption: Optional[str],
    ) -> DailyMenus:
        # Solo un pendiente a la vez por negocio: "si el admin manda una
        # imagen nueva mientras hay un pendiente, se descarta el anterior".
        self.discard_pending_if_any(business_id)

        draft = DailyMenus()
        draft.business_id = business_id
        draft.admin_phone = admin_phone
        draft.schema_version = SCHEMA_VERSION
        draft.fecha_vigencia = fecha_vigencia
        draft.status = DailyMenuStatus.PENDIENTE_CONFIRMACION.value
        draft.source_media_id = source_media_id
        draft.source_image_path = source_image_path
        draft.source_sha256 = source_sha256
        draft.caption = caption
        draft.extracted_menu = extracted_menu.model_dump()
        draft.correction_history = []
        draft.created_at = datetime.now(timezone.utc)
        draft.save()
        return draft

    def apply_correction(
        self, draft: DailyMenus, correction_text: str, updated_menu: ExtractedMenu
    ) -> DailyMenus:
        draft.extracted_menu = updated_menu.model_dump()
        history = list(draft.correction_history or [])
        history.append(correction_text)
        draft.correction_history = history
        self._crud.update(draft)
        return draft

    def retire_today(self, business_id: str, fecha_vigencia: str) -> None:
        """Saca de circulación el menú del día (activo y pendiente): el
        negocio pasó a atender con carta (ver orchestrator._apply_carta)."""
        self._replace_active(business_id, fecha_vigencia)
        self.discard_pending_if_any(business_id)

    def confirm(self, draft: DailyMenus) -> DailyMenus:
        self._replace_active(draft.business_id, draft.fecha_vigencia, keep_id=draft.daily_menu_id)
        self._reopen(draft.business_id, draft.fecha_vigencia)
        draft.status = DailyMenuStatus.ACTIVO.value
        draft.confirmed_at = datetime.now(timezone.utc)
        self._crud.update(draft)
        return draft

    def reuse_last_menu(
        self, business_id: str, fecha_vigencia: str, admin_phone: Optional[str]
    ) -> tuple[Optional[DailyMenus], Optional[str]]:
        """Deja activo para `fecha_vigencia` una copia del último menú
        confirmado ("usa el de ayer"). (menú, None) o (None, motivo):
        "ya_hay_menu_hoy" o "sin_menu_anterior"."""
        if self.get_active_for_date(business_id, fecha_vigencia) is not None:
            return None, "ya_hay_menu_hoy"
        last = self.get_last_active(business_id)
        if last is None:
            return None, "sin_menu_anterior"
        self.discard_pending_if_any(business_id)

        menu = DailyMenus()
        menu.business_id = business_id
        menu.admin_phone = admin_phone
        menu.schema_version = last.schema_version
        menu.fecha_vigencia = fecha_vigencia
        menu.status = DailyMenuStatus.PENDIENTE_CONFIRMACION.value
        menu.source_media_id = last.source_media_id
        menu.source_image_path = last.source_image_path
        menu.source_sha256 = last.source_sha256
        # ponytail: WhatsApp borra el media subido a los 30 días; si el último
        # menú es más viejo, el cliente no recibe foto (el texto sí sirve).
        menu.menu_media_id = last.menu_media_id
        menu.extracted_menu = last.extracted_menu
        menu.correction_history = []
        menu.created_at = datetime.now(timezone.utc)
        menu.save()
        return self.confirm(menu), None

    def cancel(self, draft: DailyMenus) -> DailyMenus:
        draft.status = DailyMenuStatus.DESCARTADO.value
        draft.discarded_at = datetime.now(timezone.utc)
        self._crud.update(draft)
        return draft

    def mark_closed_today(
        self,
        business_id: str,
        fecha_vigencia: str,
        admin_phone: Optional[str],
        motivo: Optional[str],
    ) -> DailyMenus:
        """El admin avisó que el negocio no abre ese día -- guarda un
        registro simple (sin extracted_menu) para que get_active_menu/
        get_today_menu puedan avisarle a quien pregunte por el menú, en vez
        de responder "no hay menú" a secas (que la IA de clientes podría
        interpretar como "todavía no lo subieron" y ofrecer esperar o pedir
        un humano por falta de datos, en vez de decir que hoy no abren).

        Descarta cualquier borrador pendiente y reemplaza un menú ya
        confirmado ese día, si lo había -- un día no puede estar "cerrado"
        y con un menú activo a la vez. Si ya se había marcado cerrado antes
        (ej. dos avisos seguidos del admin), actualiza el motivo en vez de
        duplicar el registro.
        """
        self.discard_pending_if_any(business_id)

        self._replace_active(business_id, fecha_vigencia)

        existing_closed = self.get_closed_for_date(business_id, fecha_vigencia)
        if existing_closed is not None:
            existing_closed.caption = motivo
            existing_closed.admin_phone = admin_phone or existing_closed.admin_phone
            self._crud.update(existing_closed)
            return existing_closed

        record = DailyMenus()
        record.business_id = business_id
        record.admin_phone = admin_phone
        record.schema_version = SCHEMA_VERSION
        record.fecha_vigencia = fecha_vigencia
        record.status = DailyMenuStatus.CERRADO.value
        record.caption = motivo
        record.extracted_menu = {}
        record.correction_history = []
        record.created_at = datetime.now(timezone.utc)
        record.save()
        return record

    @staticmethod
    def get_extracted_menu(draft: DailyMenus) -> ExtractedMenu:
        return ExtractedMenu.model_validate(draft.extracted_menu)
