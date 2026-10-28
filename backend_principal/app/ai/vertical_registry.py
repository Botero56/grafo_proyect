"""
Registro de verticales de negocio.

Cada vertical (restaurante, odontología, ...) expone dos funciones: cómo
obtener el agente ya construido para un rol (`get_agent`) y cómo resolver
ese rol a partir del negocio y el teléfono de quien escribe (`resolve_role`).
El webhook usa este registro para despachar sin saber nada de restaurantes
ni de odontología: solo conoce `Businesses.bussiness_vertical`.

Agregar una vertical nueva es: escribir su módulo con la misma forma que
`app.ai.ai_restaurant` (get_agent + role_resolver) y registrar una entrada
acá. No requiere tocar el router del webhook.
"""

from dataclasses import dataclass
from typing import Callable

from google.adk.agents import Agent

from app.ai import ai_restaurant
from app.ai.ai_restaurant import role_resolver as restaurant_role_resolver
from app.schemas.enum_schemas import BussinessVertical


@dataclass
class VerticalHandlers:
    get_agent: Callable[[str], Agent]
    resolve_role: Callable[..., dict]


_VERTICALS: dict[str, VerticalHandlers] = {
    BussinessVertical.RESTAURANT.value: VerticalHandlers(
        get_agent=ai_restaurant.get_agent,
        resolve_role=restaurant_role_resolver.resolve_user_role,
    ),
}


def get_vertical_handlers(vertical: str) -> VerticalHandlers:
    try:
        return _VERTICALS[vertical]
    except KeyError:
        raise ValueError(f"Vertical no registrada: {vertical!r}") from None


def is_registered(vertical: str) -> bool:
    """Si la vertical tiene agente: una sin registrar no contesta nunca."""
    return vertical in _VERTICALS
