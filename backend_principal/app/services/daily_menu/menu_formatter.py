"""JSON extraído (esquema menu_del_dia/otras_opciones) -> texto legible
para WhatsApp + el mensaje interactivo de confirmación.

WhatsApp limita un mensaje de texto a 4096 caracteres. Este formato es
compacto por diseño (dos productos, no una lista abierta de categorías),
así que en la práctica nunca se acerca al límite -- la partición que hace
`_chunk_text` es una salvaguarda, no algo que se espere usar.
"""

from datetime import date
from typing import Optional

from app.services.daily_menu.menu_diff import MenuChange, format_cop
from app.services.daily_menu.vision_extractor import ExtractedMenu
from app.services.excel_report_service.constants import MONTH_NAMES_ES, WEEKDAY_NAMES_ES

WHATSAPP_TEXT_LIMIT = 4096


def _format_money(value: Optional[float]) -> str:
    return format_cop(value, "⚠️ precio no legible")


def _weekday_and_date(fecha_vigencia: str) -> tuple[str, date]:
    day = date.fromisoformat(fecha_vigencia)
    return WEEKDAY_NAMES_ES[day.weekday()], day


def _title_for(fecha_vigencia: str) -> str:
    weekday, day = _weekday_and_date(fecha_vigencia)
    month = MONTH_NAMES_ES[day.month - 1]
    return f"*Menú del {weekday.lower()} {day.day} de {month.lower()}*"


def _incluye_text(menu: ExtractedMenu) -> Optional[str]:
    if not menu.acompanamientos:
        return None
    parts = []
    for a in menu.acompanamientos:
        if a.opciones:
            parts.append(f"{a.nombre.lower()} ({' o '.join(o.lower() for o in a.opciones)})")
        else:
            parts.append(a.nombre.lower())
    return ", ".join(parts)


def _chunk_text(text: str, limit: int = WHATSAPP_TEXT_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def format_confirmation_message(
    menu: ExtractedMenu, fecha_vigencia: str, changes: Optional[list[MenuChange]] = None
) -> list[str]:
    lines = [
        _title_for(fecha_vigencia),
        "",
        f"🍽️ *Menú del día:* {menu.menu_del_dia.nombre} — *{_format_money(menu.menu_del_dia.precio)}*",
        f"🥩 *Otras opciones:* {', '.join(menu.otras_opciones.proteinas) or 'sin proteínas detectadas'} "
        f"— *{_format_money(menu.otras_opciones.precio)}*",
    ]

    incluye = _incluye_text(menu)
    if incluye:
        lines += ["", f"*Incluye (ambas opciones):* {incluye}"]

    envios_text = "sí" if menu.envios else ("no" if menu.envios is False else "no especificado")
    contacto_text = menu.telefono_contacto or "no especificado"
    lines += ["", f"🛵 Envíos: {envios_text} · 📞 {contacto_text}"]

    if menu.advertencias:
        lines += ["", "*Advertencias:*"] + [f"⚠️ {a}" for a in menu.advertencias]

    if changes:
        lines += ["", "*Cambios respecto al anterior:*"]
        lines += [f"• {'⚠️ ' if c.warn else ''}{c.text}" for c in changes]

    full_text = "\n".join(lines)
    return _chunk_text(full_text)


def format_carta_text(items) -> list[str]:
    """La carta (FixedMenuItems) como la ven los clientes: por categoría,
    con precio."""
    by_category: dict[str, list[str]] = {}
    for item in sorted(items, key=lambda i: (i.categoria or "", i.nombre or "")):
        by_category.setdefault(item.categoria or "Otros", []).append(
            f"• {item.nombre} — {_format_money(item.precio)}"
        )
    lines = []
    for category, rows in by_category.items():
        lines += ["", f"*{category}*", *rows]
    return _chunk_text("\n".join(lines).strip())


def upload_question(fecha_menu_del_dia: Optional[str], productos_carta: int, fotos_carta: int) -> str:
    """Lo que está pendiente por guardar y la pregunta de los botones."""
    pending = []
    if fecha_menu_del_dia:
        weekday, day = _weekday_and_date(fecha_menu_del_dia)
        pending.append(f"el menú del día del {weekday.lower()} {day.day:02d}/{day.month:02d}")
    if productos_carta:
        pending.append(f"{productos_carta} productos de carta ({fotos_carta} foto(s))")
    correction = " Si algo del menú del día está mal, escríbeme la corrección." if fecha_menu_del_dia else ""
    return (
        f"Tengo listo para guardar {' y '.join(pending)}. Si hay más fotos, mándalas.{correction} "
        "¿Reemplazo todo lo de hoy o lo sumo a lo que ya hay?"
    )


def upload_buttons() -> list[dict]:
    return [
        {"id": "subida:reemplazar", "title": "🆕 Reemplazar todo"},
        {"id": "subida:agregar", "title": "➕ Sumar a lo de hoy"},
        {"id": "subida:cancelar", "title": "❌ Cancelar"},
    ]
