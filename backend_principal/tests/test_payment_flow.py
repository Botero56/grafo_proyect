"""Flujo de pago: el método queda asociado al pedido y viaja a la impresora
desde ahí (no desde el modelo); una transferencia solo se autoriza si el
valor del comprobante coincide con el total confirmado guardado."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.ai_restaurant.user.tools.customer_tools import (
    print_order_ticket,
    save_order,
    set_payment_method,
)
from app.firestore_db.firebase_schemas import Conversations, Messages, Orders, PrintJobs
from app.schemas.enum_schemas import DeliveryStatus, MessageStatus, PaymentStatus, PrintJobStatus
from app.services.payments import transfer_receipt
from app.services.payments.transfer_receipt import TransferReceiptHandler
from app.services.printer_client.printer_client import PrinterClient
from tests.conftest import (
    DummyHandlers,
    FakeToolContext,
    seed_active_menu,
    seed_business,
    seed_conversation,
    seed_customer,
    seed_printer,
)

from app.services.messaging import inbound_pipeline

TICKET = "1x Menú del día ($18500)\nTOTAL: $21000"


def _saved_order(fake_store, total=21000):
    """Pedido de dos ítems confirmado por `total`: dos menús del día de
    $9.000 (el precio sale del menú activo, no del pedido) más el domicilio
    de su zona (la diferencia)."""
    business = seed_business(fake_store)
    seed_printer(fake_store, business)
    seed_active_menu(fake_store, business, menu_del_dia={"nombre": "Menú", "precio": 9000, "tipo_precio": "fijo"})
    customer = seed_customer(fake_store, business, name="Ana")
    conversation = seed_conversation(
        fake_store,
        business,
        customer.customer_id,
        delivery_zone="Cerro Azul",
        delivery_fee=total - 18000,
        delivery_status=DeliveryStatus.ZONA.value,
    )
    ctx = FakeToolContext(business.bussiness_id, customer.customer_id)
    ctx.state["conversation_id"] = conversation.conversation_id
    item = {"product": "menu_del_dia", "qty": 1, "elecciones": {"Sopa": "Ajiaco"}}
    result = save_order({"items": [item, item], "address": "Cerro Azul torre 3"}, ctx)
    return business, customer, ctx, result["order_id"]


def _printer():
    printer_cls = patch("app.services.printer_client.print_jobs.PrinterClient")
    return printer_cls


def test_the_confirmed_total_is_stored_on_the_reference_order(fake_store):
    _, _, _, order_id = _saved_order(fake_store)

    orders = fake_store.stores[Orders]
    assert orders[order_id].order_total == 21000
    others = [o for oid, o in orders.items() if oid != order_id]
    assert [o.order_total for o in others] == [None]


def _order_context(fake_store):
    business = seed_business(fake_store)
    seed_printer(fake_store, business)
    seed_active_menu(fake_store, business)  # menú del día a $18.500
    customer = seed_customer(fake_store, business)
    conversation = seed_conversation(
        fake_store, business, customer.customer_id,
        delivery_fee=3000, delivery_status=DeliveryStatus.ZONA.value,
    )
    ctx = FakeToolContext(business.bussiness_id, customer.customer_id)
    ctx.state["conversation_id"] = conversation.conversation_id
    return ctx


def test_a_price_sent_by_the_model_is_ignored(fake_store):
    """Prompt injection: el cliente convence al modelo de guardar el menú a
    $1.000. Lo que se cobra (y contra lo que se valida la transferencia)
    sale del menú, no del pedido."""
    ctx = _order_context(fake_store)
    injected = {"product": "menu_del_dia", "qty": 2, "unit_price": 1000, "elecciones": {"Sopa": "Ajiaco"}}

    result = save_order({"items": [injected], "total": 2000}, ctx)

    order = fake_store.stores[Orders][result["order_id"]]
    assert (order.unit_price, order.order_total) == (18500, 2 * 18500 + 3000)


@pytest.mark.parametrize("qty", [0, -1, 1.5, "2", True, 21])
def test_an_invalid_quantity_saves_nothing(fake_store, qty):
    ctx = _order_context(fake_store)

    result = save_order({"items": [{"product": "menu_del_dia", "qty": qty, "elecciones": {"Sopa": "Ajiaco"}}]}, ctx)

    assert result["order_id"] is None and result["status"] == "error"
    assert fake_store.stores[Orders] == {}


def test_cash_is_stored_and_needs_no_validation(fake_store):
    _, _, ctx, order_id = _saved_order(fake_store)

    result = set_payment_method(order_id, "Efectivo", ctx)

    assert result["ok"] is True
    assert result["payment_status"] == PaymentStatus.POR_COBRAR.value
    assert fake_store.stores[Orders][order_id].payment_method == "efectivo"


def test_transfer_returns_the_exact_confirmed_amount_to_pay(fake_store):
    _, _, ctx, order_id = _saved_order(fake_store, total=21000)

    result = set_payment_method(order_id, "transferencia", ctx)

    assert result["amount_to_pay"] == 21000
    assert result["payment_status"] == PaymentStatus.PENDIENTE_COMPROBANTE.value


def test_only_cash_or_transfer_are_accepted(fake_store):
    _, _, ctx, order_id = _saved_order(fake_store)

    assert set_payment_method(order_id, "tarjeta", ctx)["ok"] is False
    assert fake_store.stores[Orders][order_id].payment_method is None


def test_a_validated_payment_cannot_be_changed(fake_store):
    _, _, ctx, order_id = _saved_order(fake_store)
    fake_store.stores[Orders][order_id].payment_status = PaymentStatus.VALIDADO.value

    assert set_payment_method(order_id, "efectivo", ctx)["ok"] is False


async def test_no_ticket_is_printed_without_a_payment_method(fake_store):
    _, _, ctx, order_id = _saved_order(fake_store)

    with _printer() as printer_cls:
        result = await print_order_ticket(order_id, TICKET, ctx)

    assert result["printed"] is False
    assert "método de pago" in result["reason"]
    printer_cls.assert_not_called()
    assert fake_store.stores[PrintJobs] == {}


async def test_cash_prints_right_away_sending_the_payment_method(fake_store):
    _, _, ctx, order_id = _saved_order(fake_store)
    set_payment_method(order_id, "efectivo", ctx)

    with _printer() as printer_cls:
        printer_cls.return_value.print_text = AsyncMock(return_value=True)
        result = await print_order_ticket(order_id, TICKET, ctx)

    assert result["printed"] is True
    text, method = printer_cls.return_value.print_text.await_args.args
    assert method == "efectivo"
    assert text.endswith("Pago: EFECTIVO\n" + "═" * 20)
    job = fake_store.stores[PrintJobs][result["print_job_id"]]
    assert job.status == PrintJobStatus.PRINTED.value
    assert job.attempts == 1


async def test_the_ticket_follows_the_kitchen_format(fake_store):
    _, _, ctx, order_id = _saved_order(fake_store)
    set_payment_method(order_id, "efectivo", ctx)
    items = "1x Menú del día\n\n1x Otra opción:\nPollo\nArroz\n\nNota:\nSin cebolla."

    with _printer() as printer_cls:
        printer_cls.return_value.print_text = AsyncMock(return_value=True)
        await print_order_ticket(order_id, items, ctx)

    thick, thin = "═" * 20, "─" * 20
    assert printer_cls.return_value.print_text.await_args.args[0] == (
        f"{thick}\nEDY GOURMET\nCOMANDA\n{thick}\n\n"
        f"{thin}\n>>>   # 1   <<<\n{thin}\n\n"
        f"PEDIDO #{order_id[-6:].upper()}\n\n"
        "Cliente: Ana\nTel: 300 123 4567\n\n"
        "Dirección: Cerro Azul torre 3\nBarrio: Cerro Azul\n\n"
        f"PEDIDO\n{items}\n\n"
        f"{thin}\nDomicilio: $3.000\nTotal: $21.000\n\n"
        f"Pago: EFECTIVO\n{thick}"
    )


async def test_transfer_holds_the_ticket_until_payment_is_validated(fake_store):
    _, _, ctx, order_id = _saved_order(fake_store)
    set_payment_method(order_id, "transferencia", ctx)

    with _printer() as printer_cls:
        result = await print_order_ticket(order_id, TICKET, ctx)

    assert result["printed"] is False
    assert result["held"] is True
    printer_cls.assert_not_called()
    assert fake_store.stores[PrintJobs][result["print_job_id"]].status == PrintJobStatus.PENDING.value


async def test_printer_payload_carries_the_payment_method_next_to_the_text():
    response = MagicMock()
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)) as post:
        assert await PrinterClient("https://printer/print").print_text(TICKET, "transferencia")

    assert post.await_args.kwargs["json"] == {"text": TICKET, "payment_method": "transferencia"}


# --- Comprobante de transferencia ------------------------------------------


def _receipt_message(fake_store, business, customer):
    message = Messages()
    message.business_id = business.bussiness_id
    message.customer_id = customer.customer_id
    message.type = "image"
    message.media_id = "media-1"
    message.mime_type = "image/jpeg"
    message.content = "[Imagen recibida]"
    return fake_store.seed(message)


def _reads(valor, referencia=None, fecha=None):
    """Lo que Gemini "leyó" en el comprobante."""
    return patch.object(
        transfer_receipt,
        "read_transfer_receipt",
        return_value=transfer_receipt.TransferReceipt(
            es_comprobante=True, valor=valor, referencia=referencia, fecha=fecha
        ),
    )


def _whatsapp():
    client = MagicMock()
    client.download_media = AsyncMock(return_value=b"jpeg")
    client.send_message = AsyncMock(return_value=True)
    return client


async def _pending_transfer_with_held_ticket(fake_store):
    business, customer, ctx, order_id = _saved_order(fake_store, total=21000)
    set_payment_method(order_id, "transferencia", ctx)
    with _printer():
        held = await print_order_ticket(order_id, TICKET, ctx)
    return business, customer, order_id, held["print_job_id"]


async def test_matching_receipt_validates_and_releases_the_held_ticket(fake_store):
    business, customer, order_id, job_id = await _pending_transfer_with_held_ticket(fake_store)
    client = _whatsapp()

    with _reads(21000.0), _printer() as printer_cls:
        printer_cls.return_value.print_text = AsyncMock(return_value=True)
        handled = await TransferReceiptHandler(client).maybe_handle(
            business, customer, _receipt_message(fake_store, business, customer)
        )

    assert handled is True
    order = fake_store.stores[Orders][order_id]
    assert order.payment_status == PaymentStatus.VALIDADO.value
    assert order.transfer_amount == 21000
    text, method = printer_cls.return_value.print_text.await_args.args
    assert method == "transferencia"
    assert text.endswith("Pago: TRANSFERENCIA ✓\n" + "═" * 20)
    assert fake_store.stores[PrintJobs][job_id].status == PrintJobStatus.PRINTED.value
    assert "Pago validado" in client.send_message.await_args.args[1]
    assert "preparación" in client.send_message.await_args.args[1]


def _rest_answer(fake_store, business, customer, order_id, answer):
    message = Messages()
    message.business_id = business.bussiness_id
    message.customer_id = customer.customer_id
    message.type = "interactive"
    message.content = f"resto:{order_id}:{answer}"
    return fake_store.seed(message)


async def _underpaid_order(fake_store, paid=15000.0):
    """Pedido de $21.000 con una transferencia de `paid` (de menos)."""
    business, customer, order_id, job_id = await _pending_transfer_with_held_ticket(fake_store)
    client = _whatsapp()
    client.send_interactive_buttons = AsyncMock(return_value="wamid.BTN")
    with _reads(paid), _printer() as printer_cls:
        await TransferReceiptHandler(client).maybe_handle(
            business, customer, _receipt_message(fake_store, business, customer)
        )
        printer_cls.assert_not_called()
    return business, customer, order_id, job_id, client


async def test_paying_less_asks_if_the_rest_is_paid_in_cash(fake_store):
    """Casi siempre que transfieren de menos es porque el resto lo pagan en
    efectivo: se pregunta antes de molestar a un humano."""
    _, _, order_id, job_id, client = await _underpaid_order(fake_store, paid=15000.0)

    assert fake_store.stores[Orders][order_id].payment_status == PaymentStatus.PENDIENTE_VALIDACION.value
    assert fake_store.stores[PrintJobs][job_id].status == PrintJobStatus.PENDING.value  # no sale aún
    _, body, buttons = client.send_interactive_buttons.await_args.args
    assert "$6.000" in body and "efectivo" in body
    assert [b["id"] for b in buttons] == [f"resto:{order_id}:efectivo", f"resto:{order_id}:no"]
    assert all(len(b["title"]) <= 20 for b in buttons)


async def test_rest_in_cash_validates_and_prints_how_much_to_collect(fake_store):
    business, customer, order_id, job_id, client = await _underpaid_order(fake_store, paid=15000.0)

    with _printer() as printer_cls:
        printer_cls.return_value.print_text = AsyncMock(return_value=True)
        handled = await TransferReceiptHandler(client).maybe_handle(
            business, customer, _rest_answer(fake_store, business, customer, order_id, "efectivo")
        )

    assert handled is True  # no le llega a la IA
    order = fake_store.stores[Orders][order_id]
    assert (order.payment_status, order.cash_due) == (PaymentStatus.VALIDADO.value, 6000)
    # Se guarda como transferencia, con la nota del resto en efectivo.
    assert order.payment_method == "transferencia"
    assert order.payment_note == "Pagó $15.000 por transferencia y el restante ($6.000) en efectivo al recibir."
    text, method = printer_cls.return_value.print_text.await_args.args
    assert method == "transferencia"
    assert text.endswith("Pago: TRANSFERENCIA ✓\nCobrar en efectivo: $6.000\n" + "═" * 20)
    assert fake_store.stores[PrintJobs][job_id].status == PrintJobStatus.PRINTED.value
    assert "$6.000 en efectivo" in client.send_message.await_args.args[1]


async def test_no_sends_the_payment_to_a_human(fake_store):
    business, customer, order_id, _, client = await _underpaid_order(fake_store)

    with patch.object(
        transfer_receipt.HandoffService, "notify_human", new=AsyncMock(return_value=True)
    ) as notify, _printer() as printer_cls:
        await TransferReceiptHandler(client).maybe_handle(
            business, customer, _rest_answer(fake_store, business, customer, order_id, "no")
        )

    printer_cls.assert_not_called()
    assert fake_store.stores[Orders][order_id].payment_status == PaymentStatus.PENDIENTE_VALIDACION.value
    assert notify.await_args.kwargs["motivo"] == "pago_por_validar"


async def test_if_the_question_cannot_be_sent_a_human_reviews_it(fake_store):
    business, customer, order_id, _ = await _pending_transfer_with_held_ticket(fake_store)
    client = _whatsapp()
    client.send_interactive_buttons = AsyncMock(return_value=None)

    with _reads(15000.0), patch.object(
        transfer_receipt.HandoffService, "notify_human", new=AsyncMock(return_value=True)
    ) as notify:
        await TransferReceiptHandler(client).maybe_handle(
            business, customer, _receipt_message(fake_store, business, customer)
        )

    notify.assert_awaited_once()  # el pedido no queda trabado en silencio


async def test_only_the_owner_can_answer_and_only_once(fake_store):
    business, customer, order_id, _, client = await _underpaid_order(fake_store)
    stranger = seed_customer(fake_store, business, phone="573009999999")
    handler = TransferReceiptHandler(client)

    await handler.maybe_handle(business, stranger, _rest_answer(fake_store, business, stranger, order_id, "efectivo"))
    assert fake_store.stores[Orders][order_id].cash_due is None  # no es su pedido

    with _printer() as printer_cls:
        printer_cls.return_value.print_text = AsyncMock(return_value=True)
        await handler.maybe_handle(business, customer, _rest_answer(fake_store, business, customer, order_id, "efectivo"))
        with patch.object(transfer_receipt.HandoffService, "notify_human", new=AsyncMock()) as notify:
            await handler.maybe_handle(business, customer, _rest_answer(fake_store, business, customer, order_id, "no"))

    assert fake_store.stores[Orders][order_id].payment_status == PaymentStatus.VALIDADO.value
    notify.assert_not_awaited()  # el segundo clic no cambia nada


def _tip_answer(fake_store, business, customer, order_id, answer):
    message = Messages()
    message.business_id = business.bussiness_id
    message.customer_id = customer.customer_id
    message.type = "interactive"
    message.content = f"propina:{order_id}:{answer}"
    return fake_store.seed(message)


async def _overpaid_order(fake_store, paid=25000.0):
    """Pedido de $21.000 pagado con una transferencia de `paid`."""
    business, customer, order_id, job_id = await _pending_transfer_with_held_ticket(fake_store)
    client = _whatsapp()
    client.send_interactive_buttons = AsyncMock(return_value="wamid.BTN")
    with _reads(paid, referencia="m 1234-56"), _printer() as printer_cls:
        printer_cls.return_value.print_text = AsyncMock(return_value=True)
        await TransferReceiptHandler(client).maybe_handle(
            business, customer, _receipt_message(fake_store, business, customer)
        )
    return business, customer, order_id, job_id, client


async def test_paying_more_validates_and_asks_if_the_difference_is_a_tip(fake_store):
    """El pedido ya está cubierto: la comanda sale igual, y la diferencia
    se le pregunta al cliente en vez de mandarla a revisión."""
    _, _, order_id, job_id, client = await _overpaid_order(fake_store, paid=25000.0)

    order = fake_store.stores[Orders][order_id]
    assert order.payment_status == PaymentStatus.VALIDADO.value
    assert (order.transfer_amount, order.transfer_excess, order.tip_amount) == (25000, 4000, None)
    assert order.transfer_reference == "M123456"
    assert fake_store.stores[PrintJobs][job_id].status == PrintJobStatus.PRINTED.value
    to, body, buttons = client.send_interactive_buttons.await_args.args
    assert "$4.000" in body and "propina" in body
    assert [b["id"] for b in buttons] == [f"propina:{order_id}:si", f"propina:{order_id}:no"]
    assert all(len(b["title"]) <= 20 for b in buttons)  # límite de WhatsApp


async def test_an_exact_payment_does_not_ask_about_a_tip(fake_store):
    _, _, order_id, _, client = await _overpaid_order(fake_store, paid=21000.0)

    assert fake_store.stores[Orders][order_id].transfer_excess is None
    client.send_interactive_buttons.assert_not_awaited()


async def test_yes_records_the_difference_as_a_tip(fake_store):
    business, customer, order_id, _, client = await _overpaid_order(fake_store)

    handled = await TransferReceiptHandler(client).maybe_handle(
        business, customer, _tip_answer(fake_store, business, customer, order_id, "si")
    )

    assert handled is True  # no le llega a la IA
    assert fake_store.stores[Orders][order_id].tip_amount == 4000
    assert "gracias" in client.send_message.await_args.args[1].lower()


async def test_no_asks_the_team_to_return_the_difference(fake_store):
    business, customer, order_id, _, client = await _overpaid_order(fake_store)

    with patch.object(
        transfer_receipt.HandoffService, "notify_human", new=AsyncMock(return_value=True)
    ) as notify:
        await TransferReceiptHandler(client).maybe_handle(
            business, customer, _tip_answer(fake_store, business, customer, order_id, "no")
        )

    assert fake_store.stores[Orders][order_id].tip_amount == 0
    assert notify.await_args.kwargs["motivo"] == "devolver_excedente"
    assert "$4.000" in notify.await_args.kwargs["detalle"]


async def test_only_the_first_answer_of_the_order_owner_counts(fake_store):
    business, customer, order_id, _, client = await _overpaid_order(fake_store)
    stranger = seed_customer(fake_store, business, phone="573009999999")
    handler = TransferReceiptHandler(client)

    await handler.maybe_handle(business, stranger, _tip_answer(fake_store, business, stranger, order_id, "si"))
    assert fake_store.stores[Orders][order_id].tip_amount is None  # no es su pedido

    await handler.maybe_handle(business, customer, _tip_answer(fake_store, business, customer, order_id, "no"))
    await handler.maybe_handle(business, customer, _tip_answer(fake_store, business, customer, order_id, "si"))
    assert fake_store.stores[Orders][order_id].tip_amount == 0  # el segundo clic no cambia nada


async def test_unreadable_receipt_asks_for_another_photo(fake_store):
    business, customer, order_id, _ = await _pending_transfer_with_held_ticket(fake_store)
    client = _whatsapp()

    with patch.object(transfer_receipt, "read_transfer_receipt", return_value=None):
        handled = await TransferReceiptHandler(client).maybe_handle(
            business, customer, _receipt_message(fake_store, business, customer)
        )

    assert handled is True
    assert fake_store.stores[Orders][order_id].payment_status == PaymentStatus.PENDIENTE_COMPROBANTE.value
    assert "foto más clara" in client.send_message.await_args.args[1]


async def test_an_image_without_a_pending_transfer_follows_the_normal_flow(fake_store):
    business = seed_business(fake_store)
    customer = seed_customer(fake_store, business)
    client = _whatsapp()

    handled = await TransferReceiptHandler(client).maybe_handle(
        business, customer, _receipt_message(fake_store, business, customer)
    )

    assert handled is False
    client.download_media.assert_not_awaited()


async def test_the_webhook_hands_the_receipt_to_the_validator_not_to_the_ai(fake_store):
    business, customer, order_id, _ = await _pending_transfer_with_held_ticket(fake_store)
    conversation = next(iter(fake_store.stores[Conversations].values()))
    message = _receipt_message(fake_store, business, customer)
    message.conversation_id = conversation.conversation_id
    result = {"business": business, "customer": customer, "conversation": conversation,
              "message": message, "message_delay_seconds": None}

    with patch.object(
        inbound_pipeline, "get_vertical_handlers", return_value=DummyHandlers("cliente", customer.customer_id)
    ), patch.object(
        inbound_pipeline.TransferReceiptHandler, "maybe_handle", new=AsyncMock(return_value=True)
    ) as maybe_handle:
        pending = await inbound_pipeline._process_customer_message(result)

    assert pending is None  # no se agenda respuesta de la IA
    maybe_handle.assert_awaited_once()
    assert message.status == MessageStatus.COMPLETED.value


class _FakeGenaiClient:
    """Como google.genai.Client: al liberarse cierra su conexión, y un
    request con la conexión cerrada revienta."""

    def __init__(self):
        self.models = _FakeModels()

    def __del__(self):
        self.models.closed = True


class _FakeModels:
    closed = False

    def generate_content(self, **kwargs):
        if self.closed:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        return MagicMock(text='{"es_comprobante": true, "valor": 28500}')


def test_receipt_reading_keeps_the_genai_client_alive_during_the_request():
    """Regresión (prueba real 2026-09-30): con el cliente creado y usado en la
    misma expresión, Python lo liberaba al tomar `.models`, se cerraba la
    conexión y la foto del comprobante nunca llegaba a Gemini."""
    with patch.object(transfer_receipt, "build_genai_client", side_effect=_FakeGenaiClient):
        assert transfer_receipt.read_transfer_receipt(b"jpeg", "image/jpeg").valor == 28500
