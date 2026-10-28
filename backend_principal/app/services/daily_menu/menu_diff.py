"""Compara el menú recién extraído contra el último menú ACTIVO del mismo
negocio (si existe), para señalarle al admin qué cambió antes de que
confirme.

El menú del día cambia todos los días -- eso es esperado, se muestra sin
advertencia. Los acompañamientos y el precio del menú del día también se
muestran normal. Las proteínas/precio de "otras opciones", los envíos y el
teléfono de contacto casi nunca cambian, así que cualquier cambio ahí se
marca con ⚠️ para que el admin lo note antes de guardar por error.
"""

from dataclasses import dataclass
from typing import Optional

from app.services.daily_menu.vision_extractor import Acompanamiento, ExtractedMenu


@dataclass
class MenuChange:
    text: str
    warn: bool


def format_cop(value: Optional[float], none_text: str) -> str:
    """Pesos con punto de miles ($18.500), o `none_text` si no hay precio."""
    return f"${value:,.0f}".replace(",", ".") if value is not None else none_text


def format_fee(value: Optional[float], none_text: str) -> str:
    """Valor del domicilio: "gratis" si la zona no cobra (tarifa 0)."""
    return "gratis" if value == 0 else format_cop(value, none_text)


def _format_money(value: Optional[float]) -> str:
    return format_cop(value, "sin precio")


def _acompanamientos_key(items: list[Acompanamiento]) -> tuple:
    return tuple(sorted((a.nombre.strip().lower(), tuple(o.strip().lower() for o in a.opciones)) for a in items))


def diff_menus(previous: Optional[ExtractedMenu], current: ExtractedMenu) -> list[MenuChange]:
    """Sin menú anterior (primera carga del negocio), no hay nada que
    comparar -- lista vacía, y el formatter no muestra la sección."""
    if previous is None:
        return []

    changes: list[MenuChange] = []

    if previous.menu_del_dia.nombre != current.menu_del_dia.nombre:
        changes.append(
            MenuChange(
                f'Menú del día: antes "{previous.menu_del_dia.nombre}", ahora '
                f'"{current.menu_del_dia.nombre}"',
                warn=False,
            )
        )
    if previous.menu_del_dia.precio != current.menu_del_dia.precio:
        changes.append(
            MenuChange(
                f"Precio del menú del día: antes {_format_money(previous.menu_del_dia.precio)}, "
                f"ahora {_format_money(current.menu_del_dia.precio)}",
                warn=False,
            )
        )

    if _acompanamientos_key(previous.acompanamientos) != _acompanamientos_key(current.acompanamientos):
        changes.append(MenuChange("Cambiaron los acompañamientos del día", warn=False))

    previous_proteinas = {p.strip().lower() for p in previous.otras_opciones.proteinas}
    current_proteinas = {p.strip().lower() for p in current.otras_opciones.proteinas}
    if previous_proteinas != current_proteinas:
        changes.append(
            MenuChange(
                f"Proteínas de otras opciones: antes {', '.join(previous.otras_opciones.proteinas) or 'ninguna'}, "
                f"ahora {', '.join(current.otras_opciones.proteinas) or 'ninguna'}",
                warn=True,
            )
        )
    if previous.otras_opciones.precio != current.otras_opciones.precio:
        changes.append(
            MenuChange(
                f"Precio de otras opciones: antes {_format_money(previous.otras_opciones.precio)}, "
                f"ahora {_format_money(current.otras_opciones.precio)}",
                warn=True,
            )
        )

    if previous.envios != current.envios:
        changes.append(
            MenuChange(f"Envíos: antes {previous.envios}, ahora {current.envios}", warn=True)
        )
    if previous.telefono_contacto != current.telefono_contacto:
        changes.append(
            MenuChange(
                f"Teléfono de contacto: antes {previous.telefono_contacto or 'ninguno'}, "
                f"ahora {current.telefono_contacto or 'ninguno'}",
                warn=True,
            )
        )

    return changes
