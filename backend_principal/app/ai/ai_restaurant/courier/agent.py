"""
Agente conversacional para los domiciliarios del restaurante (Google ADK).

El backend ya resolvió el rol (ver `app.ai.ai_restaurant.role_resolver`) y
los comandos claros ya los liquidó la vía rápida sin IA (ver
`app.services.delivery.couriers`): acá llega solo lo que el parser no
entendió.
"""

from google.adk.agents import Agent
from google.genai import types

from app.ai.ai_restaurant.courier.courier_tools import (
    liquidar_comandas,
    mi_resumen_de_hoy,
    reportar_incidencia,
    usar_modo_cliente,
)
from app.ai.ai_restaurant.courier.prompt import COURIER_AGENT_PROMPT, courier_agent_descr
from app.ai.core.conversation_memory import with_conversation_summary
from app.ai.core.genai_client import agent_model

courier_agent = Agent(
    name="courier_agent",
    model=agent_model(),
    description=courier_agent_descr,
    instruction=with_conversation_summary(COURIER_AGENT_PROMPT),
    generate_content_config=types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
    ),
    tools=[liquidar_comandas, mi_resumen_de_hoy, reportar_incidencia, usar_modo_cliente],
)
