"""
Agente conversacional para administradores del restaurante, construido con
Google ADK.

El rol del usuario (admin vs cliente) ya fue resuelto por el backend antes
de llegar aquí (ver `app.ai.ai_restaurant.role_resolver`); este módulo solo
construye el agente que atiende a un administrador.
"""

from google.adk.agents import Agent

from app.ai.core.conversation_memory import with_conversation_summary

from app.ai.ai_restaurant.admin.admin_tools import (
    find_orders_by_customer,
    generate_excel_report,
    get_current_datetime,
    get_customer_chat_reference,
    get_today_menu,
    mark_closed_today,
    query_orders_nl,
    reactivate_customer_chat,
    resolve_catalog_code,
    resolve_payment_agreement,
    request_stock_change,
    reuse_last_menu,
    set_delivery_quote,
    avisar_tiempo_entrega,
    add_delivery_zone,
    add_courier,
    liquidacion_domiciliarios,
    list_couriers,
    remove_courier,
    sync_catalog,
    validar_transferencia,
)
from app.services.plans import gate_tools
from app.ai.ai_restaurant.admin.prompt.restaurant_admin_agent_prompt import (
    RESTAURANT_ADMIN_AGENT_PROMPT,
    restaurant_admin_agent_descr,
)
from app.ai.core.genai_client import agent_model


admin_agent = Agent(
    name="admin_agent",
    model=agent_model(),
    description=restaurant_admin_agent_descr,
    instruction=with_conversation_summary(RESTAURANT_ADMIN_AGENT_PROMPT),
    # Las tools que el plan del negocio no incluye no corren (ver plans.py).
    before_tool_callback=gate_tools,
    tools=[
        get_current_datetime,
        get_today_menu,
        mark_closed_today,
        reuse_last_menu,
        request_stock_change,
        query_orders_nl,
        find_orders_by_customer,
        get_customer_chat_reference,
        reactivate_customer_chat,
        set_delivery_quote,
        avisar_tiempo_entrega,
        add_delivery_zone,
        list_couriers,
        add_courier,
        remove_courier,
        liquidacion_domiciliarios,
        resolve_payment_agreement,
        validar_transferencia,
        sync_catalog,
        resolve_catalog_code,
        generate_excel_report,
    ],
)
