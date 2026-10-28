"""
Agente conversacional para clientes del restaurante, construido con Google ADK.

El rol del usuario (cliente vs admin) ya fue resuelto por el backend antes
de llegar aquí (ver `app.ai.ai_restaurant.role_resolver`); este módulo solo
construye el agente que atiende a un cliente.
"""

from google.adk.agents import Agent
from google.genai import types

from app.ai.core.conversation_memory import append_current_state, with_conversation_summary
from app.config import ai_restaurant_settings

from app.ai.ai_restaurant.user.tools.customer_tools import (
    get_active_menu,
    report_delivery_update,
    request_human_handoff,
    responder_incidencia,
    send_menu_image,
)
from app.ai.ai_restaurant.user.tools.order_tools import confirm_order, render_state, update_order
from app.ai.ai_restaurant.user.prompt.restaurant_user_agent_prompt import (
    RESTAURANT_AGENT_PROMPT,
    restaurant_agent_descr,
)
from app.ai.core.genai_client import agent_model
from app.services.plans import gate_tools


customer_agent = Agent(
    name="customer_agent",
    # MODEL_CUSTOMER (vacío = MODEL_RESTAURANT): el agente de más volumen
    # puede ir en un modelo más barato; si falla, el pool sigue con el resto.
    model=agent_model(ai_restaurant_settings.MODEL_CUSTOMER or None),
    description=restaurant_agent_descr,
    instruction=with_conversation_summary(RESTAURANT_AGENT_PROMPT),
    before_model_callback=append_current_state(render_state),
    before_tool_callback=gate_tools,
    # Razonamiento LOW: medido el 2026-10-05 con gemini-3.8-flash sobre 5
    # conversaciones reales (pedido, tiquetera, handoff, duda, precio
    # inyectado): misma calidad, ~33% menos tiempo por turno y ~5x menos
    # tokens de salida que el nivel por defecto. MINIMAL no lo soporta el
    # modelo (400). El admin_agent queda en el nivel por defecto.
    generate_content_config=types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
    ),
    # Pocas tools y genéricas: el pedido es estado (ver order_tools), y la
    # validación, el domicilio, el pago y la comanda los hace el código
    # dentro de update_order/confirm_order, no el modelo paso a paso.
    tools=[
        get_active_menu,
        send_menu_image,
        update_order,
        confirm_order,
        report_delivery_update,
        responder_incidencia,
        request_human_handoff,
    ],
)
