"""El encabezado de la comanda usa el nombre comercial configurado
(Configurations.restaurant_name) y, sin él, el nombre del negocio."""

from app.firestore_db.firebase_schemas import Orders
from app.services.printer_client.print_jobs import build_ticket
from tests.conftest import seed_business, seed_configuration


def _header(fake_store, business):
    order = Orders()
    order.order_id = "abc123"
    return build_ticket(business, None, order, "1x Nachos").split("\n")[1]


def test_ticket_uses_the_configured_restaurant_name(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, restaurant_name="Deliperritos")

    assert _header(fake_store, business) == "DELIPERRITOS"


def test_ticket_falls_back_to_the_business_name(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)

    assert _header(fake_store, business) == "EDY GOURMET"
