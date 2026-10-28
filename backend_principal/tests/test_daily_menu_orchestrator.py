import asyncio
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.firestore_db.firebase_schemas import Customers, FixedMenuItems, Messages
from app.schemas.enum_schemas import DailyMenuStatus
from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia
from app.services.daily_menu.daily_menu_service import DailyMenuService
from app.services.daily_menu.media_downloader import DownloadedMenuImage
from app.services.daily_menu.orchestrator import MenuIngestionOrchestrator
from app.services.daily_menu.vision_extractor import Acompanamiento, CartaItem, ExtractedMenu, ExtractedUpload, MenuDelDia, OtrasOpciones
from tests.conftest import make_menu, seed_business, seed_configuration, seed_customer

ADMIN_PHONE = "573001111111"


class FakeWhatsAppClient:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, str]] = []
        self.sent_buttons: list[tuple[str, str, list]] = []

    async def send_message(self, to, text):
        self.sent_messages.append((to, text))
        return True

    async def send_interactive_buttons(self, to, body_text, buttons):
        self.sent_buttons.append((to, body_text, buttons))
        return "wamid.BTN1"

    async def upload_media(self, file_bytes, filename, mime_type):
        return "MEDIA_CARTA"


def _not_a_menu() -> ExtractedMenu:
    return ExtractedMenu(
        menu_del_dia=MenuDelDia(nombre="", precio=None, tipo_precio="no_legible"),
        acompanamientos=[],
        otras_opciones=OtrasOpciones(proteinas=[], precio=None, tipo_precio="no_legible"),
        envios=None,
        telefono_contacto=None,
        fecha_detectada=None,
        advertencias=["La imagen no parece ser un menú."],
        confianza=0.1,
    )


def _seed_admin(fake_store, business, phone=ADMIN_PHONE) -> Customers:
    return seed_customer(fake_store, business, phone, name="Admin")


def _image_message(fake_store, business, customer, media_id="media_1", content="") -> Messages:
    message = Messages()
    message.business_id = business.bussiness_id
    message.customer_id = customer.customer_id
    message.type = "image"
    message.content = content
    message.media_id = media_id
    return fake_store.seed(message)


def _button_message(fake_store, business, customer, button_id) -> Messages:
    message = Messages()
    message.business_id = business.bussiness_id
    message.customer_id = customer.customer_id
    message.type = "interactive"
    message.content = button_id
    return fake_store.seed(message)


def _text_message(fake_store, business, customer, text) -> Messages:
    message = Messages()
    message.business_id = business.bussiness_id
    message.customer_id = customer.customer_id
    message.type = "text"
    message.content = text
    return fake_store.seed(message)


def _fake_downloaded() -> DownloadedMenuImage:
    return DownloadedMenuImage(file_path=Path("fake.jpg"), mime_type="image/jpeg", sha256="abc123", data=b"fake")


def _patched_downloader():
    return patch(
        "app.services.daily_menu.orchestrator.MenuImageDownloader",
        **{"return_value.download": AsyncMock(return_value=_fake_downloaded())},
    )


@pytest.mark.asyncio
async def test_normal_load_and_confirm(fake_store):
    business = seed_business(fake_store)
    admin = _seed_admin(fake_store, business)
    message = _image_message(fake_store, business, admin)
    client = FakeWhatsAppClient()

    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=make_menu())
    ):
        handled = await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message)

    assert handled is True
    draft = DailyMenuService().get_pending(business.bussiness_id)
    assert draft is not None
    assert client.sent_buttons, "debe mandar el mensaje interactivo de confirmación"
    _, _, buttons = client.sent_buttons[-1]
    assert [b["id"] for b in buttons] == ["subida:reemplazar", "subida:agregar", "subida:cancelar"]
    assert any("Pollo al curry" in text for _, text in client.sent_messages)

    handled2 = await MenuIngestionOrchestrator(client).maybe_handle(
        business, admin, _button_message(fake_store, business, admin, "subida:agregar")
    )

    assert handled2 is True
    assert draft.status == DailyMenuStatus.ACTIVO.value
    assert "así quedó el menú de hoy" in client.sent_messages[-1][1]
    assert "Pollo al curry" in client.sent_messages[-1][1]


@pytest.mark.asyncio
async def test_only_menu_del_dia_changes_confirmation_has_no_warning(fake_store):
    business = seed_business(fake_store)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()

    message1 = _image_message(fake_store, business, admin, media_id="m1")
    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image",
        return_value=ExtractedUpload(menu=make_menu(precio=18500)),
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message1)
    draft1 = DailyMenuService().get_pending(business.bussiness_id)
    await MenuIngestionOrchestrator(client).maybe_handle(
        business, admin, _button_message(fake_store, business, admin, f"menu:{draft1.daily_menu_id}:confirm")
    )

    client.sent_messages.clear()
    message2 = _image_message(fake_store, business, admin, media_id="m2")
    new_menu = make_menu(precio=18500)
    new_menu.menu_del_dia.nombre = "Sancocho de gallina"
    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=new_menu)
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message2)

    confirmation_text = next(text for _, text in client.sent_messages if text.startswith("*Menú del"))
    assert "Cambios respecto al anterior" in confirmation_text
    assert "Pollo al curry con champiñones" in confirmation_text
    assert "Sancocho de gallina" in confirmation_text
    # Cambio de menú del día: se muestra, pero sin ⚠️ en esa línea (la
    # línea del diff, no el renglón del cuerpo "🍽️ *Menú del día:*").
    change_lines = [l for l in confirmation_text.splitlines() if "Menú del día: antes" in l]
    assert change_lines and "⚠️" not in change_lines[0]


@pytest.mark.asyncio
async def test_otras_opciones_price_change_is_flagged_with_warning(fake_store):
    business = seed_business(fake_store)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()

    message1 = _image_message(fake_store, business, admin, media_id="m1")
    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image",
        return_value=ExtractedUpload(menu=make_menu(otras_precio=20500)),
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message1)
    draft1 = DailyMenuService().get_pending(business.bussiness_id)
    await MenuIngestionOrchestrator(client).maybe_handle(
        business, admin, _button_message(fake_store, business, admin, f"menu:{draft1.daily_menu_id}:confirm")
    )

    client.sent_messages.clear()
    message2 = _image_message(fake_store, business, admin, media_id="m2")
    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image",
        return_value=ExtractedUpload(menu=make_menu(otras_precio=22000)),
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message2)

    confirmation_text = next(text for _, text in client.sent_messages if text.startswith("*Menú del"))
    # "Precio de otras opciones:" es la línea del diff (distinta del
    # renglón del cuerpo "🥩 *Otras opciones:*") -- esa es la que debe
    # llevar ⚠️.
    change_lines = [l for l in confirmation_text.splitlines() if "Precio de otras opciones" in l]
    assert change_lines and "⚠️" in change_lines[0]


@pytest.mark.asyncio
async def test_correction_removes_protein_then_reconfirm(fake_store):
    business = seed_business(fake_store)
    admin = _seed_admin(fake_store, business)
    message = _image_message(fake_store, business, admin)
    client = FakeWhatsAppClient()

    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=make_menu())
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message)
    draft = DailyMenuService().get_pending(business.bussiness_id)

    correct_click = _button_message(fake_store, business, admin, f"menu:{draft.daily_menu_id}:correct")
    await MenuIngestionOrchestrator(client).maybe_handle(business, admin, correct_click)

    correction_text = _text_message(fake_store, business, admin, "hoy no hay chicharrón")
    updated = make_menu(proteinas=["Cerdo", "Pollo", "Res"])
    with patch("app.services.daily_menu.orchestrator.apply_correction", return_value=updated):
        handled = await MenuIngestionOrchestrator(client).maybe_handle(business, admin, correction_text)

    assert handled is True
    assert "Chicharrón" not in draft.extracted_menu["otras_opciones"]["proteinas"]
    assert draft.correction_history == ["hoy no hay chicharrón"]

    reconfirm = _button_message(fake_store, business, admin, f"menu:{draft.daily_menu_id}:confirm")
    await MenuIngestionOrchestrator(client).maybe_handle(business, admin, reconfirm)
    assert draft.status == DailyMenuStatus.ACTIVO.value


@pytest.mark.asyncio
async def test_correction_sopa_solo_ajiaco(fake_store):
    business = seed_business(fake_store)
    admin = _seed_admin(fake_store, business)
    message = _image_message(fake_store, business, admin)
    client = FakeWhatsAppClient()

    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=make_menu())
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message)
    draft = DailyMenuService().get_pending(business.bussiness_id)

    correction_text = _text_message(fake_store, business, admin, "hoy la sopa es solo ajiaco")
    updated = make_menu()
    updated.acompanamientos = [Acompanamiento(nombre="Sopa", opciones=["Ajiaco"]), Acompanamiento(nombre="Arroz", opciones=[])]
    with patch("app.services.daily_menu.orchestrator.apply_correction", return_value=updated):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, correction_text)

    sopa = next(a for a in draft.extracted_menu["acompanamientos"] if a["nombre"] == "Sopa")
    assert sopa["opciones"] == ["Ajiaco"]


@pytest.mark.asyncio
async def test_cancel_discards_draft(fake_store):
    business = seed_business(fake_store)
    admin = _seed_admin(fake_store, business)
    message = _image_message(fake_store, business, admin)
    client = FakeWhatsAppClient()

    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=make_menu())
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message)

    draft = DailyMenuService().get_pending(business.bussiness_id)
    cancel_click = _button_message(fake_store, business, admin, f"menu:{draft.daily_menu_id}:cancel")
    await MenuIngestionOrchestrator(client).maybe_handle(business, admin, cancel_click)

    assert draft.status == DailyMenuStatus.DESCARTADO.value


@pytest.mark.asyncio
async def test_second_image_same_day_replaces_active_menu(fake_store):
    business = seed_business(fake_store)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()

    message1 = _image_message(fake_store, business, admin, media_id="m1")
    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=make_menu(precio=18500))
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message1)
    draft1 = DailyMenuService().get_pending(business.bussiness_id)
    confirm1 = _button_message(fake_store, business, admin, f"menu:{draft1.daily_menu_id}:confirm")
    await MenuIngestionOrchestrator(client).maybe_handle(business, admin, confirm1)
    assert draft1.status == DailyMenuStatus.ACTIVO.value

    message2 = _image_message(fake_store, business, admin, media_id="m2")
    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=make_menu(precio=99000))
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message2)
    draft2 = DailyMenuService().get_pending(business.bussiness_id)
    assert draft2.daily_menu_id != draft1.daily_menu_id
    assert draft1.status == DailyMenuStatus.ACTIVO.value

    confirm2 = _button_message(fake_store, business, admin, f"menu:{draft2.daily_menu_id}:confirm")
    await MenuIngestionOrchestrator(client).maybe_handle(business, admin, confirm2)

    assert draft1.status == DailyMenuStatus.REEMPLAZADO.value
    assert draft2.status == DailyMenuStatus.ACTIVO.value


@pytest.mark.asyncio
async def test_new_image_discards_pending_draft(fake_store):
    business = seed_business(fake_store)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()

    message1 = _image_message(fake_store, business, admin, media_id="m1")
    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=make_menu())
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message1)
    draft1 = DailyMenuService().get_pending(business.bussiness_id)

    message2 = _image_message(fake_store, business, admin, media_id="m2")
    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=make_menu(precio=30000))
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message2)

    assert draft1.status == DailyMenuStatus.DESCARTADO.value


@pytest.mark.asyncio
async def test_image_with_manana_caption_sets_tomorrow_as_vigencia(fake_store):
    business = seed_business(fake_store)
    admin = _seed_admin(fake_store, business)
    message = _image_message(fake_store, business, admin, content="menú de mañana")
    client = FakeWhatsAppClient()

    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=make_menu())
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message)

    draft = DailyMenuService().get_pending(business.bussiness_id)
    from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia

    assert draft.fecha_vigencia == resolve_fecha_vigencia("mañana")


@pytest.mark.asyncio
async def test_date_mismatch_adds_warning(fake_store):
    from datetime import date

    from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia
    from app.services.excel_report_service.constants import WEEKDAY_NAMES_ES

    business = seed_business(fake_store)
    admin = _seed_admin(fake_store, business)
    message = _image_message(fake_store, business, admin)
    client = FakeWhatsAppClient()

    today = resolve_fecha_vigencia(None)
    actual_weekday = WEEKDAY_NAMES_ES[date.fromisoformat(today).weekday()]
    wrong_weekday = next(w for w in WEEKDAY_NAMES_ES if w != actual_weekday)

    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image",
        return_value=ExtractedUpload(menu=make_menu(fecha_detectada=wrong_weekday.upper())),
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message)

    draft = DailyMenuService().get_pending(business.bussiness_id)
    assert any(wrong_weekday.upper() in w for w in draft.extracted_menu["advertencias"])


@pytest.mark.asyncio
async def test_image_that_is_not_a_menu_creates_no_draft(fake_store):
    business = seed_business(fake_store)
    admin = _seed_admin(fake_store, business)
    message = _image_message(fake_store, business, admin)
    client = FakeWhatsAppClient()

    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=_not_a_menu())
    ):
        handled = await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message)

    assert handled is True
    assert DailyMenuService().get_pending(business.bussiness_id) is None
    assert any("no parece ser un menú" in text.lower() for _, text in client.sent_messages)


@pytest.mark.asyncio
async def test_stale_confirmation_of_old_menu_id_is_a_safe_noop(fake_store):
    business = seed_business(fake_store)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()

    message1 = _image_message(fake_store, business, admin, media_id="m1")
    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=make_menu())
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message1)
    old_draft = DailyMenuService().get_pending(business.bussiness_id)

    message2 = _image_message(fake_store, business, admin, media_id="m2")
    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=make_menu(precio=30000))
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message2)
    new_draft = DailyMenuService().get_pending(business.bussiness_id)
    assert old_draft.status == DailyMenuStatus.DESCARTADO.value

    stale_confirm = _button_message(fake_store, business, admin, f"menu:{old_draft.daily_menu_id}:confirm")
    await MenuIngestionOrchestrator(client).maybe_handle(business, admin, stale_confirm)

    assert old_draft.status == DailyMenuStatus.DESCARTADO.value
    assert new_draft.status == DailyMenuStatus.PENDIENTE_CONFIRMACION.value


@pytest.mark.asyncio
async def test_concurrent_images_for_same_business_are_serialized(fake_store):
    """Dos imágenes casi simultáneas del mismo negocio NUNCA deben
    procesarse en paralelo -- si no, ambas podrían ver "no hay pendiente"
    antes de que la otra termine de crear el suyo (bug real, ver
    orchestrator._business_locks)."""
    business = seed_business(fake_store)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()

    lock = threading.Lock()
    state = {"concurrent": 0, "max_concurrent": 0}

    def slow_extract(*args, **kwargs):
        with lock:
            state["concurrent"] += 1
            state["max_concurrent"] = max(state["max_concurrent"], state["concurrent"])
        time.sleep(0.1)
        with lock:
            state["concurrent"] -= 1
        return ExtractedUpload(menu=make_menu())

    message1 = _image_message(fake_store, business, admin, media_id="m1")
    message2 = _image_message(fake_store, business, admin, media_id="m2")

    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", side_effect=slow_extract
    ):
        await asyncio.gather(
            MenuIngestionOrchestrator(client).maybe_handle(business, admin, message1),
            MenuIngestionOrchestrator(client).maybe_handle(business, admin, message2),
        )

    assert state["max_concurrent"] == 1

    # Resultado funcional: solo queda UN pendiente (el de la segunda
    # imagen, que descartó el de la primera), nunca dos a la vez.
    from app.firestore_db.firebase_schemas import DailyMenus

    pending_drafts = [
        d for d in fake_store.stores[DailyMenus].values() if d.status == DailyMenuStatus.PENDIENTE_CONFIRMACION.value
    ]
    assert len(pending_drafts) == 1


@pytest.mark.asyncio
async def test_confirm_adjusts_fecha_vigencia_if_date_rolled_over_since_upload(fake_store):
    business = seed_business(fake_store)
    admin = _seed_admin(fake_store, business)
    message = _image_message(fake_store, business, admin)
    client = FakeWhatsAppClient()

    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=make_menu())
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, message)
    draft = DailyMenuService().get_pending(business.bussiness_id)
    original_fecha = draft.fecha_vigencia
    next_day = (date.fromisoformat(original_fecha) + timedelta(days=1)).isoformat()

    # Simula que la medianoche pasó entre la subida y el click de confirmar.
    with patch(
        "app.services.daily_menu.orchestrator.resolve_fecha_vigencia", return_value=next_day
    ):
        confirm_click = _button_message(fake_store, business, admin, f"menu:{draft.daily_menu_id}:confirm")
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, confirm_click)

    assert draft.fecha_vigencia == next_day
    assert draft.status == DailyMenuStatus.ACTIVO.value
    confirmation_text = next(text for _, text in client.sent_messages if "así quedó" in text)
    assert "ya cambió el día" in confirmation_text
    assert next_day in confirmation_text


async def _confirm_menu(fake_store, business, admin, client):
    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=make_menu())
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(
            business, admin, _image_message(fake_store, business, admin)
        )
    draft = DailyMenuService().get_pending(business.bussiness_id)
    click = _button_message(fake_store, business, admin, f"menu:{draft.daily_menu_id}:confirm")
    await MenuIngestionOrchestrator(client).maybe_handle(business, admin, click)
    return draft


@pytest.mark.asyncio
async def test_daily_menu_and_carta_live_together_with_agregar(fake_store):
    """Prueba real 2026-10-19: menú del día + foto de asados con "Juntar"
    borraba el menú del día. Ahora conviven."""
    business = seed_business(fake_store)
    configuration = seed_configuration(fake_store, business)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()

    menu = await _confirm_menu(fake_store, business, admin, client)
    await _send_carta_photo(fake_store, business, admin, client, [CartaItem(categoria="Asados", nombre="Churrasco", precio=40000)], "M1")
    assert menu.status == DailyMenuStatus.ACTIVO.value  # nada cambia hasta el botón
    await _click(fake_store, business, admin, client, "agregar")

    assert menu.status == DailyMenuStatus.ACTIVO.value
    assert _carta_names(fake_store) == ["Churrasco"]
    summary = "\n".join(text for _, text in client.sent_messages[-3:])
    assert "Pollo al curry" in summary and "Churrasco" in summary

    # Al cliente le llegan las dos fotos: la del menú del día y la de la carta.
    today = resolve_fecha_vigencia(None)
    _, media_ids, _ = DailyMenuService().current_menu_image(business.bussiness_id, today)
    assert media_ids == ["MEDIA_CARTA", "M1"]

    # Otro menú del día con "agregar" no borra la carta.
    await _confirm_menu(fake_store, business, admin, client)
    assert _carta_names(fake_store) == ["Churrasco"]
    assert configuration.carta_media_id == "M1"


@pytest.mark.asyncio
async def test_reemplazar_with_only_a_daily_menu_removes_the_carta(fake_store):
    business = seed_business(fake_store)
    configuration = seed_configuration(fake_store, business)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()
    await _send_carta_photo(fake_store, business, admin, client, [CartaItem(categoria="Asados", nombre="Churrasco", precio=40000)], "M1")
    await _click(fake_store, business, admin, client, "agregar")

    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=ExtractedUpload(menu=make_menu())
    ):
        await MenuIngestionOrchestrator(client).maybe_handle(
            business, admin, _image_message(fake_store, business, admin)
        )
    await _click(fake_store, business, admin, client, "reemplazar")

    assert not fake_store.stores[FixedMenuItems]
    assert configuration.carta_media_id is None
    assert DailyMenuService().get_active_for_date(business.bussiness_id, resolve_fecha_vigencia(None))


async def _send_carta_photo(fake_store, business, admin, client, productos, media_id):
    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image",
        return_value=ExtractedUpload(menu=_not_a_menu(), productos=productos),
    ) as daily, patch.object(client, "upload_media", AsyncMock(return_value=media_id)):
        await MenuIngestionOrchestrator(client).maybe_handle(
            business, admin, _image_message(fake_store, business, admin, media_id=media_id)
        )
    return daily


async def _click(fake_store, business, admin, client, action):
    button = next(b["id"] for b in client.sent_buttons[-1][2] if b["id"].endswith(action))
    await MenuIngestionOrchestrator(client).maybe_handle(
        business, admin, _button_message(fake_store, business, admin, button)
    )


def _carta_names(fake_store):
    return sorted(i.nombre for i in fake_store.stores[FixedMenuItems].values())


@pytest.mark.asyncio
async def test_carta_in_several_photos_then_new_menu_replaces_the_old_one(fake_store):
    business = seed_business(fake_store)
    configuration = seed_configuration(fake_store, business)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()

    await _send_carta_photo(fake_store, business, admin, client, [CartaItem(categoria="Bebidas", nombre="Agua", precio=5000)], "M1")
    await _click(fake_store, business, admin, client, "reemplazar")
    assert _carta_names(fake_store) == ["Agua"]

    # Carta nueva en dos fotos: nada cambia hasta el botón.
    await _send_carta_photo(fake_store, business, admin, client, [CartaItem(categoria="Perros", nombre="Perro", precio=12000)], "M2")
    await _send_carta_photo(fake_store, business, admin, client, [CartaItem(categoria="Postres", nombre="Flan", precio=6000)], "M3")
    assert "2 productos de carta (2 foto(s))" in client.sent_buttons[-1][1]
    assert _carta_names(fake_store) == ["Agua"]

    await _click(fake_store, business, admin, client, "reemplazar")
    assert _carta_names(fake_store) == ["Flan", "Perro"]
    assert configuration.carta_media_id == "M2"
    _, media_ids, _ = DailyMenuService().current_menu_image(business.bussiness_id, resolve_fecha_vigencia(None))
    assert media_ids == ["M2", "M3"]


@pytest.mark.asyncio
async def test_carta_merge_keeps_the_previous_products_and_photos(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()

    await _send_carta_photo(fake_store, business, admin, client, [CartaItem(categoria="Bebidas", nombre="Agua", precio=5000)], "M1")
    await _click(fake_store, business, admin, client, "reemplazar")
    await _send_carta_photo(
        fake_store, business, admin, client,
        [CartaItem(categoria="Bebidas", nombre="Agua", precio=6000), CartaItem(categoria="Postres", nombre="Flan", precio=6000)],
        "M2",
    )
    await _click(fake_store, business, admin, client, "agregar")

    items = {i.nombre: i.precio for i in fake_store.stores[FixedMenuItems].values()}
    assert items == {"Agua": 6000, "Flan": 6000}
    _, media_ids, _ = DailyMenuService().current_menu_image(business.bussiness_id, resolve_fecha_vigencia(None))
    assert media_ids == ["M1", "M2"]


@pytest.mark.asyncio
async def test_carta_cancel_and_stale_button_change_nothing(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()

    await _send_carta_photo(fake_store, business, admin, client, [CartaItem(categoria="Bebidas", nombre="Agua", precio=5000)], "M1")
    stale_buttons = client.sent_buttons[-1][2]
    await _click(fake_store, business, admin, client, "cancelar")
    assert not fake_store.stores[FixedMenuItems]

    await MenuIngestionOrchestrator(client).maybe_handle(
        business, admin, _button_message(fake_store, business, admin, stale_buttons[0]["id"])
    )
    assert not fake_store.stores[FixedMenuItems]
    assert "pendiente" in client.sent_messages[-1][1]


async def _active_carta(fake_store, business, admin, client):
    await _send_carta_photo(fake_store, business, admin, client, [CartaItem(categoria="Perros", nombre="Perro", precio=12000)], "M1")
    await _send_carta_photo(fake_store, business, admin, client, [CartaItem(categoria="Bebidas", nombre="Agua", precio=5000)], "M2")
    await _click(fake_store, business, admin, client, "reemplazar")


@pytest.mark.asyncio
async def test_after_saving_the_admin_sees_the_full_carta_and_is_asked_catalog_or_photo(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()

    await _active_carta(fake_store, business, admin, client)

    summary = client.sent_messages[-1][1]
    assert "así quedó el menú de hoy" in summary and "• Agua — $5.000" in summary and "• Perro — $12.000" in summary
    assert [b["title"] for b in client.sent_buttons[-1][2]] == ["📒 Catálogo", "🖼️ Menú en foto"]


@pytest.mark.asyncio
async def test_catalog_sends_the_link_instead_of_the_screenshots(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()
    await _active_carta(fake_store, business, admin, client)
    today = resolve_fecha_vigencia(None)

    await _click(fake_store, business, admin, client, "catalogo")
    assert "Mándame el link" in client.sent_messages[-1][1]
    # Sin link todavía no se manda nada (ni las capturas).
    assert DailyMenuService().current_menu_image(business.bussiness_id, today) is None

    # Un texto cualquiera sigue a la IA; el link se guarda.
    assert not await MenuIngestionOrchestrator(client).maybe_handle(
        business, admin, _text_message(fake_store, business, admin, "cuántos pedidos van?")
    )
    assert await MenuIngestionOrchestrator(client).maybe_handle(
        business, admin, _text_message(fake_store, business, admin, "este es: https://wa.me/c/573001234567.")
    )
    _, media_ids, link = DailyMenuService().current_menu_image(business.bussiness_id, today)
    assert (media_ids, link) == ([], "https://wa.me/c/573001234567")

    # Ya guardado, otro link no se toma como catálogo (va a la IA).
    assert not await MenuIngestionOrchestrator(client).maybe_handle(
        business, admin, _text_message(fake_store, business, admin, "mira https://otra.com")
    )


@pytest.mark.asyncio
async def test_menu_photo_keeps_sending_the_photos(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()
    await _active_carta(fake_store, business, admin, client)

    await _click(fake_store, business, admin, client, "foto")

    _, media_ids, link = DailyMenuService().current_menu_image(business.bussiness_id, resolve_fecha_vigencia(None))
    assert (media_ids, link) == (["M1", "M2"], None)


@pytest.mark.asyncio
async def test_admin_today_menu_shows_the_carta_when_there_is_no_daily_menu(fake_store):
    from app.ai.ai_restaurant.admin.admin_tools import get_today_menu
    from tests.conftest import FakeToolContext

    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()
    await _active_carta(fake_store, business, admin, client)

    result = get_today_menu(FakeToolContext(business.bussiness_id))

    assert result["carta_available"] is True
    assert "• Perro — $12.000" in result["text"]


@pytest.mark.asyncio
async def test_one_read_per_photo_with_daily_menu_and_carta_together(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()
    upload = ExtractedUpload(menu=make_menu(), productos=[CartaItem(categoria="Asados", nombre="Churrasco", precio=40000)])

    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", return_value=upload
    ) as read, patch.object(client, "upload_media", AsyncMock(return_value="FOTO")) as uploaded:
        await MenuIngestionOrchestrator(client).maybe_handle(
            business, admin, _image_message(fake_store, business, admin)
        )
    assert read.call_count == 1 and uploaded.await_count == 1
    assert "el menú del día" in client.sent_buttons[-1][1] and "1 productos de carta" in client.sent_buttons[-1][1]

    await _click(fake_store, business, admin, client, "agregar")

    assert _carta_names(fake_store) == ["Churrasco"]
    # La misma foto no se le manda dos veces al cliente.
    _, media_ids, _ = DailyMenuService().current_menu_image(business.bussiness_id, resolve_fecha_vigencia(None))
    assert media_ids == ["FOTO"]


@pytest.mark.asyncio
async def test_catalog_answer_also_applies_to_a_daily_menu_only_upload(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()
    await _confirm_menu(fake_store, business, admin, client)
    assert [b["title"] for b in client.sent_buttons[-1][2]] == ["📒 Catálogo", "🖼️ Menú en foto"]

    await _click(fake_store, business, admin, client, "catalogo")
    await MenuIngestionOrchestrator(client).maybe_handle(
        business, admin, _text_message(fake_store, business, admin, "https://wa.me/c/57300")
    )

    _, media_ids, link = DailyMenuService().current_menu_image(business.bussiness_id, resolve_fecha_vigencia(None))
    assert (media_ids, link) == ([], "https://wa.me/c/57300")


@pytest.mark.asyncio
async def test_a_batch_of_photos_asks_once_and_queued_clicks_stay_quiet(fake_store):
    """Prueba real 2026-10-21: 10 fotos de catálogo seguidas -> 10 juegos de
    botones; el admin apretó varios, quedaron en fila detrás de las fotos y al
    final llegaron ~8 "No hay nada pendiente" seguidos."""
    from datetime import datetime, timezone

    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    admin = _seed_admin(fake_store, business)
    client = FakeWhatsAppClient()

    def slow_extract(*args, **kwargs):
        time.sleep(0.05)
        return ExtractedUpload(menu=_not_a_menu(), productos=[CartaItem(categoria="Carta", nombre="Agua", precio=5000)])

    photos = [_image_message(fake_store, business, admin, media_id=f"m{i}") for i in range(4)]
    with _patched_downloader(), patch(
        "app.services.daily_menu.orchestrator.extract_upload_from_image", side_effect=slow_extract
    ):
        await asyncio.gather(*(MenuIngestionOrchestrator(client).maybe_handle(business, admin, m) for m in photos))

    upload_prompts = [b for b in client.sent_buttons if b[2][0]["id"] == "subida:reemplazar"]
    assert len(upload_prompts) == 1 and "4 foto(s)" in upload_prompts[0][1]

    # Tres clics que llegaron antes de que se guardara: solo el primero responde.
    early = datetime.now(timezone.utc)
    clicks = [_button_message(fake_store, business, admin, "subida:agregar") for _ in range(3)]
    for click in clicks:
        click.created_at = early
    for click in clicks:
        await MenuIngestionOrchestrator(client).maybe_handle(business, admin, click)
    assert not any("nada pendiente" in text for _, text in client.sent_messages)

    # Un clic suelto más tarde sí avisa.
    await MenuIngestionOrchestrator(client).maybe_handle(
        business, admin, _button_message(fake_store, business, admin, "subida:agregar")
    )
    assert "nada pendiente" in client.sent_messages[-1][1]
