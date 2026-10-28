from datetime import datetime

from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia
from app.services.excel_report_service.constants import DEFAULT_TZ


def test_no_caption_defaults_to_today():
    now = datetime(2026, 9, 21, 10, 0, tzinfo=DEFAULT_TZ)
    assert resolve_fecha_vigencia(None, now=now) == "2026-09-21"
    assert resolve_fecha_vigencia("", now=now) == "2026-09-21"


def test_manana_keyword_resolves_to_tomorrow():
    now = datetime(2026, 9, 21, 10, 0, tzinfo=DEFAULT_TZ)
    assert resolve_fecha_vigencia("menú de mañana", now=now) == "2026-09-22"
    assert resolve_fecha_vigencia("Menu de MANANA porfa", now=now) == "2026-09-22"


def test_explicit_date_in_caption():
    now = datetime(2026, 9, 21, 10, 0, tzinfo=DEFAULT_TZ)
    assert resolve_fecha_vigencia("menú del 25/09", now=now) == "2026-09-25"


def test_explicit_past_date_rolls_to_next_year():
    now = datetime(2026, 12, 20, 10, 0, tzinfo=DEFAULT_TZ)
    assert resolve_fecha_vigencia("menú del 02/01", now=now) == "2027-01-02"


def test_unrelated_caption_defaults_to_today():
    now = datetime(2026, 9, 21, 10, 0, tzinfo=DEFAULT_TZ)
    assert resolve_fecha_vigencia("con mucho sabor", now=now) == "2026-09-21"
