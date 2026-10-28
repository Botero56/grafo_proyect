from app.services.daily_menu.menu_diff import MenuChange
from app.services.daily_menu.menu_formatter import (
    upload_buttons,
    upload_question,
    format_confirmation_message,
)
from app.services.daily_menu.vision_extractor import Acompanamiento
from tests.conftest import make_menu


def _menu(**overrides):
    # "Torta de queso" aparte: test_message_lists_incluye_with_options la busca.
    acompanamientos = [
        Acompanamiento(nombre="Sopa", opciones=["Ajiaco", "Frijoles"]),
        Acompanamiento(nombre="Torta de queso", opciones=[]),
        Acompanamiento(nombre="Arroz", opciones=[]),
    ]
    return make_menu(acompanamientos=acompanamientos, **overrides)


def test_message_contains_plato_y_otras_opciones_con_precio():
    text = format_confirmation_message(_menu(), "2026-09-21")[0]
    assert "Pollo al curry con champiñones" in text
    assert "$18.500" in text
    assert "Cerdo, Chicharrón, Pollo, Res" in text
    assert "$20.500" in text


def test_message_lists_incluye_with_options():
    text = format_confirmation_message(_menu(), "2026-09-21")[0]
    assert "sopa (ajiaco o frijoles)" in text
    assert "torta de queso" in text
    assert "arroz" in text


def test_no_legible_price_is_flagged():
    text = format_confirmation_message(_menu(tipo_precio="no_legible", precio=None), "2026-09-21")[0]
    assert "⚠️ precio no legible" in text


def test_envios_and_contacto_line():
    text = format_confirmation_message(_menu(envios=True, telefono="3235099092"), "2026-09-21")[0]
    assert "Envíos: sí" in text
    assert "3235099092" in text

    text_no_envios = format_confirmation_message(_menu(envios=False, telefono=None), "2026-09-21")[0]
    assert "Envíos: no" in text_no_envios
    assert "no especificado" in text_no_envios


def test_title_has_weekday_and_date():
    text = format_confirmation_message(_menu(), "2026-09-21")[0]
    assert "*Menú del lunes 21 de septiembre*" in text


def test_changes_section_only_appears_with_changes():
    text_no_changes = format_confirmation_message(_menu(), "2026-09-21", changes=[])[0]
    assert "Cambios respecto al anterior" not in text_no_changes

    changes = [
        MenuChange('Menú del día: antes "Sopa de arroz", ahora "Pollo al curry con champiñones"', warn=False),
        MenuChange("Precio de otras opciones: antes $19.000, ahora $20.500", warn=True),
    ]
    text_with_changes = format_confirmation_message(_menu(), "2026-09-21", changes=changes)[0]
    assert "*Cambios respecto al anterior:*" in text_with_changes
    assert '• Menú del día: antes "Sopa de arroz"' in text_with_changes
    assert "• ⚠️ Precio de otras opciones" in text_with_changes


def test_upload_buttons_fit_whatsapp_limits():
    buttons = upload_buttons()
    assert [b["id"] for b in buttons] == ["subida:reemplazar", "subida:agregar", "subida:cancelar"]
    for b in buttons:
        assert len(b["title"]) <= 20


def test_upload_question_lists_what_is_pending():
    body = upload_question("2026-09-21", 12, 2)
    assert "el menú del día del lunes 21/09 y 12 productos de carta (2 foto(s))" in body
    assert len(body) <= 1024
