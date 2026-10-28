from app.ai.ai_restaurant.admin.admin_tools import reactivate_customer_chat
from tests.conftest import FakeToolContext, seed_business, seed_conversation, seed_customer

PHONE = "573009999999"


def _seed_customer_and_conversation(fake_store, business, *, bot_activo, phone=PHONE, name="Camilo"):
    customer = seed_customer(fake_store, business, phone, name=name)
    conversation = seed_conversation(fake_store, business, customer.customer_id, bot_activo=bot_activo)
    return customer, conversation


def test_reactivates_a_paused_conversation(fake_store):
    business = seed_business(fake_store)
    customer, conversation = _seed_customer_and_conversation(fake_store, business, bot_activo=False)

    result = reactivate_customer_chat(FakeToolContext(business.bussiness_id), PHONE)

    assert result == {"reactivated": True, "customer_name": "Camilo"}
    assert conversation.bot_activo is True
    assert conversation.motivo_handoff is None


def test_reports_not_paused_when_already_active(fake_store):
    business = seed_business(fake_store)
    _seed_customer_and_conversation(fake_store, business, bot_activo=True)

    result = reactivate_customer_chat(FakeToolContext(business.bussiness_id), PHONE)

    assert result == {"reactivated": False, "reason": "no_estaba_pausado"}


def test_reports_customer_not_found(fake_store):
    business = seed_business(fake_store)

    result = reactivate_customer_chat(FakeToolContext(business.bussiness_id), "573001112222")

    assert result == {"reactivated": False, "reason": "cliente_no_encontrado"}
