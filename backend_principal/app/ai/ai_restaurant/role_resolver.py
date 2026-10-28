"""
Resolución de rol (admin/cliente) del restaurante.

Esto NO es una tool del agente: por seguridad, el rol de quien escribe lo
determina el backend antes de invocar cualquier agente, nunca el modelo.
El backend llama a `resolve_user_role` con el business_id ya identificado
(por el webhook) y usa el resultado para decidir qué agente de
`app.ai.ai_restaurant.get_agent` invocar.
"""

from app.firestore_db.firebase_schemas import Configurations, Customers
from app.services import plans
from app.services.delivery.tracking import service_date
from app.services.phones import normalize_phone


def resolve_user_role(business_id: str, phone_number: str, customer_id: str = None) -> dict:
    """Determina si quien escribe es administrador, domiciliario o cliente.

    Compara el teléfono de quien escribe contra `admin_phone_numbers` y
    luego contra `couriers` de la `Configurations` del negocio (admin gana).
    Un domiciliario que hoy eligió escribir como cliente resuelve a
    "cliente" con `courier=True`: sus comandos de liquidación se siguen
    atendiendo y el resto va al agente de clientes. Si no hay
    Configurations, todo el mundo resuelve a "cliente" (comportamiento
    seguro por defecto).

    Args:
        business_id: ID del negocio (ya resuelto por el webhook).
        phone_number: teléfono de quien escribe. Se compara normalizado
            (solo dígitos, con indicativo) contra la lista, también
            normalizada: "+57 300..." y "300..." son el mismo admin.
        customer_id: ID del customer ya resuelto por el webhook (si existe),
            para no volver a consultarlo.

    Returns:
        dict con:
            - role: "admin", "domiciliario" o "cliente"
            - customer_id: el customer_id recibido, o None si es admin.
            - courier: True si es un domiciliario en modo cliente.
    """
    configuration = Configurations.collection.filter(business_id=business_id).get()
    admin_phone_numbers = configuration.admin_phone_numbers if configuration else []
    phone = normalize_phone(phone_number)

    if phone in {normalize_phone(p) for p in admin_phone_numbers or []}:
        return {"role": "admin", "customer_id": None}

    couriers = (configuration.couriers if configuration else None) or []
    # Sin el plan de domiciliarios, quien esté en la lista escribe como
    # cliente: ni el bot de domiciliarios ni la vía rápida de liquidación.
    if phone in {normalize_phone(c.get("phone")) for c in couriers} and plans.allows(business_id, plans.DOMICILIARIOS):
        customer = Customers.collection.get(customer_id) if customer_id else None
        if customer is not None and customer.courier_as_customer_date == service_date(business_id):
            return {"role": "cliente", "customer_id": customer_id, "courier": True}
        return {"role": "domiciliario", "customer_id": customer_id}

    return {"role": "cliente", "customer_id": customer_id}
