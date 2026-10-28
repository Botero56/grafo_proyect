"""Acuerdos especiales de pago (tiquetera, pago mensual, ya pagado...): la IA
nunca decide si un pedido se cobra. Le pregunta a la administradora y la
comanda espera su respuesta."""

from unittest.mock import AsyncMock, patch

from app.ai.ai_restaurant.admin.admin_tools import resolve_payment_agreement
from app.ai.ai_restaurant.user.tools.customer_tools import (
    print_order_ticket,
    request_payment_agreement,
    set_payment_method,
)
from app.firestore_db.firebase_schemas import Orders, PrintJobs
from app.schemas.enum_schemas import PaymentStatus, PrintJobStatus
from app.services.payments import payment_agreement
from tests.conftest import FakeToolContext, seed_customer
from tests.test_payment_flow import TICKET, _printer, _saved_order


def _notify():
    return patch.object(payment_agreement.HandoffService, "notify_human", new=AsyncMock(return_value=True))


async def _pending_agreement(fake_store):
    """Pedido cuyo cliente dijo "tengo tiquetera", con la comanda armada."""
    business, customer, ctx, order_id = _saved_order(fake_store)
    with _notify() as notify:
        result = await request_payment_agreement(order_id, "tengo tiquetera", ctx)
    with _printer() as printer_cls:
        ticket = await print_order_ticket(order_id, TICKET, ctx)
        printer_cls.assert_not_called()
    return business, customer, ctx, order_id, result, notify, ticket


def _admin_side_effects():
    """WhatsApp hacia el cliente y la nota en su sesión de IA."""
    client = patch.object(payment_agreement, "WhatsAppClient")
    note = patch.object(payment_agreement.shared_agent_runner(), "append_human_message", new=AsyncMock())
    return client, note


async def test_a_special_agreement_waits_for_the_admin_instead_of_assuming(fake_store):
    _, customer, _, order_id, result, notify, ticket = await _pending_agreement(fake_store)

    assert result == {"requested": True, "admin_notified": True}
    order = fake_store.stores[Orders][order_id]
    assert (order.payment_method, order.payment_status) == ("acuerdo", PaymentStatus.PENDIENTE_AUTORIZACION.value)
    assert order.payment_agreement == "tengo tiquetera"
    detalle = notify.await_args.kwargs["detalle"]
    assert "tengo tiquetera" in detalle and f"no cobrar {customer.phone_number}" in detalle
    # La comanda NO sale hasta que la administradora decida.
    assert ticket["held"] is True and "administración" in ticket["reason"]


async def test_admin_says_do_not_charge_prints_the_ticket_marked_no_charge(fake_store):
    business, customer, _, order_id, *_ = await _pending_agreement(fake_store)
    client, note = _admin_side_effects()

    with client as client_cls, note as add_note, _printer() as printer_cls:
        client_cls.for_business.return_value.send_message = AsyncMock(return_value=True)
        printer_cls.return_value.print_text = AsyncMock(return_value=True)
        result = await resolve_payment_agreement(FakeToolContext(business.bussiness_id), customer.phone_number, False)

    assert result["ok"] is True and result["customer_notified"] is True
    assert fake_store.stores[Orders][order_id].payment_status == PaymentStatus.SIN_COBRO.value
    text, method = printer_cls.return_value.print_text.await_args.args
    assert "Pago: NO COBRAR (acuerdo con la administración)" in text and method == "acuerdo"
    sent = client_cls.for_business.return_value.send_message.await_args.args[1]
    assert "no se cobra" in sent
    add_note.assert_awaited_once()  # la IA del cliente sabe la decisión


async def test_admin_says_charge_goes_back_to_the_normal_payment(fake_store):
    business, customer, ctx, order_id, *_ = await _pending_agreement(fake_store)
    client, note = _admin_side_effects()

    with client as client_cls, note, _printer() as printer_cls:
        client_cls.for_business.return_value.send_message = AsyncMock(return_value=True)
        await resolve_payment_agreement(FakeToolContext(business.bussiness_id), customer.phone_number, True)
        printer_cls.assert_not_called()

    order = fake_store.stores[Orders][order_id]
    assert (order.payment_method, order.payment_status) == (None, None)
    # La comanda retenida se cancela: la IA arma una nueva al elegir el método.
    assert [j.status for j in fake_store.stores[PrintJobs].values()] == [PrintJobStatus.CANCELLED.value]
    assert "efectivo o por transferencia" in client_cls.for_business.return_value.send_message.await_args.args[1]
    assert set_payment_method(order_id, "efectivo", ctx)["ok"] is True


def test_the_customer_cannot_choose_agreement_as_a_payment_method(fake_store):
    _, _, ctx, order_id = _saved_order(fake_store)

    assert set_payment_method(order_id, "acuerdo", ctx)["ok"] is False


async def test_a_no_charge_order_cannot_be_switched_to_another_method(fake_store):
    business, customer, ctx, order_id, *_ = await _pending_agreement(fake_store)
    client, note = _admin_side_effects()
    with client as client_cls, note, _printer() as printer_cls:
        client_cls.for_business.return_value.send_message = AsyncMock(return_value=True)
        printer_cls.return_value.print_text = AsyncMock(return_value=True)
        await resolve_payment_agreement(FakeToolContext(business.bussiness_id), customer.phone_number, False)

    assert set_payment_method(order_id, "transferencia", ctx)["ok"] is False


async def test_admin_answer_without_a_pending_request_is_rejected(fake_store):
    business, customer, _, _ = _saved_order(fake_store)

    result = await resolve_payment_agreement(FakeToolContext(business.bussiness_id), customer.phone_number, False)

    assert result == {"ok": False, "reason": "sin_pedido_esperando_autorizacion"}


async def test_a_customer_cannot_request_an_agreement_on_someone_elses_order(fake_store):
    business, _, _, order_id = _saved_order(fake_store)
    stranger = seed_customer(fake_store, business, phone="573009999999")

    with _notify() as notify:
        result = await request_payment_agreement(
            order_id, "no me cobren", FakeToolContext(business.bussiness_id, stranger.customer_id)
        )

    assert result["requested"] is False
    notify.assert_not_awaited()
