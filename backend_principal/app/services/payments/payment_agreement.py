"""Acuerdos especiales de pago: tiquetera, pago mensual, "eso ya lo pagué",
"la administradora me dijo que no me cobraran"...

La IA nunca decide si un pedido se cobra: lo pregunta a la administradora,
igual que la cotización de un domicilio (ver app/services/delivery/quotes.py).

1. request_authorization: el cliente dice que tiene un acuerdo -> el pedido
   queda `pendiente_autorizacion` (su comanda se retiene, ver
   print_order_ticket) y se le pregunta a la administradora en su chat.
2. resolve: la administradora responde desde su chat (tool
   resolve_payment_agreement del admin) ->
   - No se cobra: `sin_cobro`, sale la comanda marcada "NO COBRAR" y se le
     avisa al cliente.
   - Sí se cobra: el pedido vuelve a no tener método de pago, la comanda
     retenida se cancela y al cliente se le pregunta efectivo o
     transferencia; la IA sigue con el cobro normal.
   En ambos casos se deja constancia en la sesión de la IA del cliente para
   que, cuando él responda, siga desde ahí.
"""

from typing import Optional

from app.ai.core.agent_runner import shared_agent_runner
from app.firestore_db.firebase_schemas import Businesses, Customers, Orders, PrintJobs
from app.schemas.enum_schemas import PaymentMethod, PaymentStatus, PrintJobStatus
from app.services.conversation.handoff_service import HandoffService
from app.services.daily_menu.menu_diff import format_cop
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.printer_client.print_jobs import release_held_ticket
from app.services.time_utils import as_utc
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient

NOT_CHARGED_TEXT = (
    "✅ La administración confirmó tu acuerdo: este pedido no se cobra. Tu pedido pasa a preparación."
)
CHARGED_TEXT = (
    "La administración nos indica que este pedido sí se cobra. "
    "¿Lo pagas en efectivo o por transferencia?"
)


async def request_authorization(
    business: Businesses, customer: Customers, order: Orders, motivo: str
) -> bool:
    """Deja el pedido esperando la decisión y le pregunta a la
    administradora. Devuelve True si el aviso salió."""
    order.payment_method = PaymentMethod.ACUERDO.value
    order.payment_status = PaymentStatus.PENDIENTE_AUTORIZACION.value
    order.payment_agreement = motivo
    FireStoreCRUDService().update(order)

    return await HandoffService().notify_human(
        business,
        customer,
        motivo="autorizar_pago",
        detalle=(
            f'Dice: "{motivo}". Pedido de {format_cop(order.order_total, "-")}. '
            f'¿Se le cobra? Respóndeme "no cobrar {customer.phone_number}" o '
            f'"cobrar {customer.phone_number}".'
        ),
    )


def find_order_awaiting_authorization(business_id: str, customer_id: str) -> Optional[Orders]:
    """El pedido más reciente del cliente que espera la decisión, o None."""
    orders = list(
        Orders.collection.filter(
            business_id=business_id,
            customer_id=customer_id,
            payment_status=PaymentStatus.PENDIENTE_AUTORIZACION.value,
        ).fetch()
    )
    if not orders:
        return None
    return max(orders, key=lambda o: as_utc(o.created_at) if o.created_at else as_utc(o.confirmed_at))


async def resolve(business: Businesses, customer: Customers, order: Orders, charge: bool) -> bool:
    """Aplica la decisión de la administradora y se la comunica al cliente.
    Devuelve True si el mensaje al cliente salió."""
    if charge:
        order.payment_method = None
        order.payment_status = None
        FireStoreCRUDService().update(order)
        # La comanda retenida ya no debe salir: la IA arma una nueva cuando
        # el cliente elija cómo pagar (print_order_ticket).
        for job in PrintJobs.collection.filter(
            order_id=order.order_id, status=PrintJobStatus.PENDING.value
        ).fetch():
            job.status = PrintJobStatus.CANCELLED.value
            FireStoreCRUDService().update(job)
        text = CHARGED_TEXT
    else:
        order.payment_status = PaymentStatus.SIN_COBRO.value
        FireStoreCRUDService().update(order)
        await release_held_ticket(order)
        text = NOT_CHARGED_TEXT

    sent = await WhatsAppClient.for_business(business).send_message(customer.phone_number, text)
    await shared_agent_runner().append_human_message(
        business_id=business.bussiness_id,
        conversation_id=order.conversation_id,
        user_id=customer.customer_id,
        text=text,
        by_system=True,
    )
    return sent
