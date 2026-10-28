"""Horario de atención de cada negocio (Configurations.opening_hours).

    {
      "timezone": "America/Bogota",
      "weekly": {"mon": [{"open": "11:00", "close": "15:00"},
                         {"open": "18:00", "close": "02:00"}],   # cruza medianoche
                 ...},
      "exceptions": [{"date": "2026-12-25", "closed": true},
                     {"date": "2026-12-31", "ranges": [{"open": "10:00", "close": "14:00"}]}],
      "enforce": true,      # true = confirm_order rechaza fuera de horario
    }

Un rango con close <= open termina al día siguiente y pertenece al día en
que empieza. Una excepción reemplaza el horario semanal de ESE día. El
"hoy no abrimos" que el admin dice por chat (mark_closed_today, un
DailyMenus CERRADO) cuenta como una excepción cerrada del día: una sola
fuente de verdad para el chat y el front.

Sin opening_hours configurado el negocio no tiene restricción (como antes),
con un warning una vez por proceso.
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.firestore_db.firebase_schemas import Configurations, DailyMenus
from app.schemas.enum_schemas import DailyMenuStatus
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery

logger = logging.getLogger("whatsapp-webhook")

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NAMES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
DEFAULT_TZ = "America/Bogota"
_CLOSED_TTL_SECONDS = 60

_warned: set[str] = set()
# (business_id, fecha) -> (vence_en, cerrado)
_closed_cache: dict[tuple[str, str], tuple[float, bool]] = {}


def _hhmm(value: str) -> tuple[int, int]:
    hours, minutes = value.split(":")
    return int(hours), int(minutes)


def _ranges_for(hours: dict, day: date, closed_days: set[str]) -> list[dict]:
    if day.isoformat() in closed_days:
        return []
    exception = next((e for e in hours.get("exceptions") or [] if e.get("date") == day.isoformat()), None)
    if exception is not None:
        return [] if exception.get("closed") else exception.get("ranges") or []
    return (hours.get("weekly") or {}).get(DAYS[day.weekday()]) or []


def _intervals(hours: dict, start_day: date, days: int, tz: ZoneInfo, closed_days: set[str]):
    """(apertura, cierre) aware, ordenados, de los rangos que EMPIEZAN entre
    start_day y start_day + days - 1."""
    out = []
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        for r in _ranges_for(hours, day, closed_days):
            opens = datetime(day.year, day.month, day.day, *_hhmm(r["open"]), tzinfo=tz)
            closes = datetime(day.year, day.month, day.day, *_hhmm(r["close"]), tzinfo=tz)
            if closes <= opens:
                closes += timedelta(days=1)
            out.append((opens, closes))
    return sorted(out)


def evaluate(hours: dict, now: datetime, closed_days: frozenset[str] = frozenset()) -> dict:
    """{"open": bool, "next_opening": datetime local | None}. Puro: sin
    Firestore, para probarlo con cualquier `now` (aware, en cualquier zona)."""
    tz = ZoneInfo(hours.get("timezone") or DEFAULT_TZ)
    local = now.astimezone(tz)
    # Desde ayer: un rango de anoche puede seguir abierto pasada la medianoche.
    intervals = _intervals(hours, local.date() - timedelta(days=1), 9, tz, set(closed_days))
    is_open = any(opens <= local < closes for opens, closes in intervals)
    upcoming = [opens for opens, _ in intervals if opens > local]
    return {"open": is_open, "next_opening": upcoming[0] if upcoming else None}


def describe_next_opening(next_opening: Optional[datetime], now: datetime) -> str:
    if next_opening is None:
        return "sin próxima apertura programada"
    local_now = now.astimezone(next_opening.tzinfo)
    delta = (next_opening.date() - local_now.date()).days
    when = "hoy" if delta == 0 else "mañana" if delta == 1 else f"el {_DAY_NAMES[next_opening.weekday()]}"
    return f"{when} a las {next_opening:%H:%M}"


def describe_weekly(hours: dict) -> str:
    """"lun 11:00-15:00 y 18:00-02:00; mar cerrado; ...\""""
    weekly = hours.get("weekly") or {}
    parts = []
    for key, name in zip(DAYS, _DAY_NAMES):
        ranges = weekly.get(key) or []
        text = " y ".join(f"{r['open']}-{r['close']}" for r in ranges) or "cerrado"
        parts.append(f"{name[:3]} {text}")
    return "; ".join(parts)


def _closed_by_admin(business_id: str, day: str) -> bool:
    """¿El admin dijo por chat que ese día no abren? Cacheado 60 s: se
    consulta en cada llamada al modelo."""
    key = (business_id, day)
    cached = _closed_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    closed = (
        DailyMenus.collection.filter(
            business_id=business_id, fecha_vigencia=day, status=DailyMenuStatus.CERRADO.value
        ).get()
        is not None
    )
    _closed_cache[key] = (time.monotonic() + _CLOSED_TTL_SECONDS, closed)
    return closed


def invalidate(business_id: str) -> None:
    for key in [k for k in _closed_cache if k[0] == business_id]:
        _closed_cache.pop(key, None)


def status(business_id: str, hours: Optional[dict] = None, now: Optional[datetime] = None) -> dict:
    """{"configured", "open", "enforce", "next_opening"} del negocio ahora.
    `hours`: el opening_hours ya leído (ej. del perfil cacheado); si no, se
    lee de Configurations."""
    if hours is None:
        configuration = TenantScopedQuery.get_one_for_business(Configurations, business_id)
        hours = (configuration.opening_hours if configuration else None) or {}
    if not hours.get("weekly") and not hours.get("exceptions"):
        if business_id not in _warned:
            _warned.add(business_id)
            logger.warning("El negocio %s no tiene horario configurado: atiende sin restricción", business_id)
        return {"configured": False, "open": True, "enforce": False, "next_opening": None}
    now = now or datetime.now(timezone.utc)
    local_today = now.astimezone(ZoneInfo(hours.get("timezone") or DEFAULT_TZ)).date()
    closed_days = frozenset(
        d.isoformat()
        for d in (local_today - timedelta(days=1), local_today, local_today + timedelta(days=1))
        if _closed_by_admin(business_id, d.isoformat())
    )
    result = evaluate(hours, now, closed_days)
    return {"configured": True, "enforce": hours.get("enforce", True), **result}
