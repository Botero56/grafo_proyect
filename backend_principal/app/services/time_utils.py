"""Fechas del proyecto: siempre en UTC y con zona.

Firestore devuelve toda fecha guardada como UTC con zona, así que el
código escribe `datetime.now(timezone.utc)` y compara contra eso. Antes se
escribía `datetime.now()` sin zona: restarlo a una fecha leída de
Firestore revienta con TypeError, y en una máquina fuera de UTC (dev en
Colombia, UTC-5) las fechas quedaban corridas 5 horas.

Puro stdlib a propósito: lo puede importar cualquier módulo, incluido el
webhook liviano.
"""

from datetime import datetime, timezone


def as_utc(value: datetime) -> datetime:
    """Una fecha sin zona se asume UTC -- es lo que hace Firestore al
    guardarla. Llega así de datos viejos, de fakes en tests o de un
    `pausado_hasta` enviado por la API sin zona."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def from_wa_timestamp(raw) -> datetime | None:
    """`timestamp` de WhatsApp (epoch en segundos, como string) -> datetime
    UTC. None si falta o no se puede leer: quien lo usa cae a "ahora"."""
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
