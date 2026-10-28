"""Resuelve la fecha_vigencia del menú del día a partir del caption que
puso el admin al mandar la foto.

Deliberadamente determinista, no LLM: esto controla el FLUJO de qué
documento se crea/actualiza (idempotencia, qué es "el menú de hoy"), así
que depender de un modelo para decidirlo sería frágil. Solo reconoce
"mañana" y fechas explícitas DD/MM o DD-MM; cualquier otra cosa (o caption
vacío) usa la fecha de hoy -- coincide con el único ejemplo dado en el
requerimiento ("menú de mañana").
"""

import re
from datetime import date, datetime
from datetime import timedelta
from typing import Optional

from app.services.excel_report_service.constants import DEFAULT_TZ

_EXPLICIT_DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})\b")
_TOMORROW_KEYWORDS = ("mañana", "manana")


def resolve_fecha_vigencia(caption: Optional[str], now: Optional[datetime] = None) -> str:
    """Devuelve 'YYYY-MM-DD' en DEFAULT_TZ (zona horaria del negocio)."""
    reference = now or datetime.now(DEFAULT_TZ)
    today = reference.date()

    if not caption:
        return today.isoformat()

    lowered = caption.lower()
    if any(keyword in lowered for keyword in _TOMORROW_KEYWORDS):
        return (today + timedelta(days=1)).isoformat()

    match = _EXPLICIT_DATE_RE.search(caption)
    if match:
        day, month = int(match.group(1)), int(match.group(2))
        try:
            candidate = date(today.year, month, day)
        except ValueError:
            return today.isoformat()
        # Si la fecha explícita ya pasó este año (ej. escriben "02/01" en
        # diciembre, para el año siguiente), asumimos el próximo año.
        if candidate < today:
            candidate = candidate.replace(year=today.year + 1)
        return candidate.isoformat()

    return today.isoformat()
