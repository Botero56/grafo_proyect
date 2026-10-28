from app.ai.ai_restaurant.user.tools.customer_tools import get_active_menu, validate_order_items
from app.firestore_db.firebase_schemas import FixedMenuItems
from app.schemas.enum_schemas import DailyMenuStatus
from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia
from app.services.daily_menu.daily_menu_service import DailyMenuService
from tests.conftest import FakeToolContext, seed_business, seed_configuration
from tests.conftest import seed_active_menu as _seed_active_daily_menu


def test_no_active_menu_reports_unavailable(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, daily_menu_expected_ready_time="09:00")

    result = get_active_menu(FakeToolContext(business.bussiness_id))

    assert result == {
        "menu_available": False,
        "closed_today": False,
        "expected_ready_time": "09:00",
        "extras_fijos": [],
    }


def test_closed_today_reports_closed_instead_of_unavailable(fake_store):
    business = seed_business(fake_store)
    DailyMenuService().mark_closed_today(
        business.bussiness_id, resolve_fecha_vigencia(None), "573001111111", "cerrado por remodelación"
    )

    result = get_active_menu(FakeToolContext(business.bussiness_id))

    assert result == {
        "menu_available": False,
        "closed_today": True,
        "closed_reason": "cerrado por remodelación",
    }


def test_closed_on_a_different_date_does_not_affect_today(fake_store):
    business = seed_business(fake_store)
    DailyMenuService().mark_closed_today(business.bussiness_id, "2000-01-01", None, None)
    _seed_active_daily_menu(fake_store, business)

    result = get_active_menu(FakeToolContext(business.bussiness_id))

    assert result["menu_available"] is True


def test_yesterdays_menu_is_not_used_today(fake_store):
    business = seed_business(fake_store)
    _seed_active_daily_menu(fake_store, business, fecha_vigencia="2000-01-01")

    result = get_active_menu(FakeToolContext(business.bussiness_id))

    assert result["menu_available"] is False


def test_active_menu_exposes_two_products_and_shared_acompanamientos(fake_store):
    business = seed_business(fake_store)
    _seed_active_daily_menu(fake_store, business)

    result = get_active_menu(FakeToolContext(business.bussiness_id))

    assert result["menu_available"] is True
    assert result["menu_del_dia"]["nombre"] == "Pollo al curry con champiñones"
    assert result["menu_del_dia"]["price"] == 18500
    assert result["otras_opciones"]["proteinas"] == ["Cerdo", "Chicharrón", "Pollo", "Res"]
    assert result["otras_opciones"]["price"] == 20500
    # Los acompañamientos son compartidos -- responden tanto "qué incluye
    # el menú del día" como "qué incluye otras opciones".
    assert {a["nombre"] for a in result["acompanamientos"]} == {"Sopa", "Arroz"}
    assert result["envios"] is True
    assert result["telefono_contacto"] == "3235099092"


def test_no_legible_price_has_no_numeric_price(fake_store):
    business = seed_business(fake_store)
    _seed_active_daily_menu(
        fake_store,
        business,
        menu_del_dia={"nombre": "Pollo al curry", "precio": None, "tipo_precio": "no_legible"},
    )

    result = get_active_menu(FakeToolContext(business.bussiness_id))

    assert result["menu_del_dia"]["price_type"] == "no_legible"
    assert result["menu_del_dia"]["price"] is None


def test_extras_fijos_are_included_but_optional(fake_store):
    business = seed_business(fake_store)
    _seed_active_daily_menu(fake_store, business)

    fixed = FixedMenuItems()
    fixed.business_id = business.bussiness_id
    fixed.nombre = "Limonada"
    fixed.tipo_precio = "fijo"
    fixed.precio = 5000
    fixed.categoria = "Bebidas"
    fixed.is_active = True
    fake_store.seed(fixed)

    result = get_active_menu(FakeToolContext(business.bussiness_id))

    assert result["extras_fijos"] == [
        {
            "id": f"fixed:{fixed.fixed_item_id}",
            "name": "Limonada",
            "description": None,
            "category": "Bebidas",
            "price_type": "fijo",
            "price": 5000,
        }
    ]


def test_validate_order_items_requires_sopa_choice_for_menu_del_dia(fake_store):
    business = seed_business(fake_store)
    _seed_active_daily_menu(fake_store, business)
    ctx = FakeToolContext(business.bussiness_id)

    result = validate_order_items([{"product": "menu_del_dia", "qty": 1}], ctx)

    assert result["valid"] is False
    assert any("Sopa" in e for e in result["errors"])


def test_validate_order_items_accepts_menu_del_dia_with_valid_choice(fake_store):
    business = seed_business(fake_store)
    _seed_active_daily_menu(fake_store, business)
    ctx = FakeToolContext(business.bussiness_id)

    result = validate_order_items(
        [{"product": "menu_del_dia", "qty": 1, "elecciones": {"Sopa": "Ajiaco"}}], ctx
    )

    assert result["valid"] is True
    assert result["resolved_items"][0]["unit_price"] == 18500
    assert result["resolved_items"][0]["elecciones"] == {"Sopa": "Ajiaco"}


def test_validate_order_items_requires_proteina_for_otras_opciones(fake_store):
    business = seed_business(fake_store)
    _seed_active_daily_menu(fake_store, business)
    ctx = FakeToolContext(business.bussiness_id)

    result = validate_order_items(
        [{"product": "otras_opciones", "qty": 1, "elecciones": {"Sopa": "Ajiaco"}}], ctx
    )

    assert result["valid"] is False
    assert any("proteína" in e.lower() for e in result["errors"])


def test_validate_order_items_rejects_protein_not_available_today(fake_store):
    business = seed_business(fake_store)
    _seed_active_daily_menu(fake_store, business)
    ctx = FakeToolContext(business.bussiness_id)

    result = validate_order_items(
        [
            {
                "product": "otras_opciones",
                "qty": 1,
                "elecciones": {"Sopa": "Ajiaco"},
                "proteina": "Camarones",
            }
        ],
        ctx,
    )

    assert result["valid"] is False
    assert any("Camarones" in e for e in result["errors"])


def test_validate_order_items_accepts_otras_opciones_with_protein_and_choice(fake_store):
    business = seed_business(fake_store)
    _seed_active_daily_menu(fake_store, business)
    ctx = FakeToolContext(business.bussiness_id)

    result = validate_order_items(
        [
            {
                "product": "otras_opciones",
                "qty": 2,
                "elecciones": {"Sopa": "Frijoles"},
                "proteina": "Pollo",
            }
        ],
        ctx,
    )

    assert result["valid"] is True
    resolved = result["resolved_items"][0]
    assert resolved["unit_price"] == 20500
    assert resolved["proteina"] == "Pollo"
    assert resolved["elecciones"] == {"Sopa": "Frijoles"}


def test_validate_order_items_rejects_no_legible_price(fake_store):
    business = seed_business(fake_store)
    _seed_active_daily_menu(
        fake_store,
        business,
        otras_opciones={"proteinas": ["Pollo"], "precio": None, "tipo_precio": "no_legible"},
    )
    ctx = FakeToolContext(business.bussiness_id)

    result = validate_order_items(
        [{"product": "otras_opciones", "qty": 1, "elecciones": {"Sopa": "Ajiaco"}, "proteina": "Pollo"}],
        ctx,
    )

    assert result["valid"] is False
    assert any("no está confirmado" in e for e in result["errors"])


def test_validation_uses_the_price_the_admin_just_corrected(fake_store):
    """Regresión (prueba real 2026-09-30): el admin corrigió otras opciones
    de $20.500 a $21.000 y el pedido siguió saliendo a $20.500, porque la IA
    le pasaba a la validación un menú viejo de su historial. La tool debe
    leer siempre el menú vigente."""
    business = seed_business(fake_store)
    old = _seed_active_daily_menu(fake_store, business)
    old.status = DailyMenuStatus.REEMPLAZADO.value
    _seed_active_daily_menu(
        fake_store,
        business,
        otras_opciones={"proteinas": ["Pollo"], "precio": 21000, "tipo_precio": "fijo"},
    )

    result = validate_order_items(
        [{"product": "otras_opciones", "qty": 1, "elecciones": {"Sopa": "Ajiaco"}, "proteina": "Pollo"}],
        FakeToolContext(business.bussiness_id),
    )

    assert result["valid"] is True
    assert result["resolved_items"][0]["unit_price"] == 21000
