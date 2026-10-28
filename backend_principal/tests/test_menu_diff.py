from app.services.daily_menu.menu_diff import diff_menus
from tests.conftest import make_menu


def test_no_previous_menu_returns_empty_diff():
    assert diff_menus(None, make_menu()) == []


def test_only_menu_del_dia_changes_no_warning():
    previous = make_menu(nombre="Sopa de arroz")
    current = make_menu(nombre="Pollo al curry")

    changes = diff_menus(previous, current)

    assert len(changes) == 1
    assert changes[0].warn is False
    assert "Sopa de arroz" in changes[0].text
    assert "Pollo al curry" in changes[0].text


def test_otras_opciones_price_change_is_flagged():
    previous = make_menu(otras_precio=19000)
    current = make_menu(otras_precio=20500)

    changes = diff_menus(previous, current)

    assert len(changes) == 1
    assert changes[0].warn is True
    assert "19.000" in changes[0].text
    assert "20.500" in changes[0].text


def test_proteinas_change_is_flagged():
    previous = make_menu(proteinas=["Cerdo", "Pollo"])
    current = make_menu(proteinas=["Cerdo", "Pollo", "Res"])

    changes = diff_menus(previous, current)

    assert any(c.warn is True and "Proteínas" in c.text for c in changes)


def test_envios_and_telefono_changes_are_flagged():
    previous = make_menu(envios=True, telefono="3235099092")
    current = make_menu(envios=False, telefono="3000000000")

    changes = diff_menus(previous, current)

    texts = [c.text for c in changes]
    assert any("Envíos" in t for t in texts)
    assert any("Teléfono" in t for t in texts)
    assert all(c.warn for c in changes)


def test_identical_menu_has_no_changes():
    menu = make_menu()
    assert diff_menus(menu, menu.model_copy(deep=True)) == []
