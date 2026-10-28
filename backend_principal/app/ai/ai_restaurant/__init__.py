"""
Punto de entrada de la vertical de restaurante.

El backend resuelve el rol del usuario (ver `role_resolver.resolve_user_role`
u otra lógica propia) ANTES de llegar aquí, por seguridad: la IA nunca decide
si quien escribe es admin o cliente. Una vez resuelto, se usa `get_agent`
para obtener el agente correspondiente.
"""

from app.ai.ai_restaurant.admin.agent import admin_agent
from app.ai.ai_restaurant.courier.agent import courier_agent
from app.ai.ai_restaurant.user.agent import customer_agent

_AGENTS_BY_ROLE = {
    "admin": admin_agent,
    "domiciliario": courier_agent,
    "cliente": customer_agent,
}


def get_agent(role: str):
    try:
        return _AGENTS_BY_ROLE[role]
    except KeyError:
        raise ValueError(f"Rol desconocido: {role!r}") from None


__all__ = ["admin_agent", "courier_agent", "customer_agent", "get_agent"]
