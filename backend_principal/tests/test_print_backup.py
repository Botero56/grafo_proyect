"""Respaldo de comandas: si la impresora falla, la comanda llega al chat de
administración para que no se pierda -- una sola vez por pedido."""

from unittest.mock import AsyncMock, patch

from app.firestore_db.firebase_schemas import Businesses, Orders, PrintJobs
from app.schemas.enum_schemas import PrintJobStatus
from app.services.printer_client import print_jobs
from app.services.printer_client.print_jobs import send_print_job
from tests.conftest import seed_business, seed_customer, seed_printer

TICKET = "1x Menú del día ($18500)\nSopa: Frijoles\nTOTAL: $21500\nDirección: Cerro Azul torre 3"


def _job(fake_store, order_id=None):
    business = seed_business(fake_store)
    seed_printer(fake_store, business)
    customer = seed_customer(fake_store, business, name="Ana")
    order = Orders()
    order.business_id = business.bussiness_id
    order.customer_id = customer.customer_id
    fake_store.seed(order)
    job = PrintJobs()
    job.business_id = business.bussiness_id
    job.order_id = order_id or order.order_id
    job.content = TICKET
    job.status = PrintJobStatus.PENDING.value
    job.attempts = 0
    return fake_store.seed(job)


def _new_job_for(fake_store, job):
    again = PrintJobs()
    again.business_id = job.business_id
    again.order_id = job.order_id
    again.content = TICKET
    again.attempts = 0
    return fake_store.seed(again)


async def _send(job, printed: bool, admin_ok=True, payment="efectivo"):
    with patch.object(print_jobs, "PrinterClient") as printer_cls, patch.object(
        print_jobs.HandoffService, "notify_admins", new=AsyncMock(return_value=admin_ok)
    ) as notify:
        printer_cls.return_value.print_text = AsyncMock(return_value=printed)
        result = await send_print_job(job, payment)
    return result, notify


async def test_successful_print_follows_the_normal_flow(fake_store):
    job = _job(fake_store)

    printed, notify = await _send(job, printed=True)

    assert printed is True
    notify.assert_not_awaited()
    assert job.status == PrintJobStatus.PRINTED.value
    assert job.admin_backup_sent_at is None


async def test_failed_print_sends_the_ticket_to_the_admin_chat(fake_store):
    job = _job(fake_store)

    printed, notify = await _send(job, printed=False)

    assert printed is False
    assert job.status == PrintJobStatus.FAILED.value
    text = notify.await_args.args[1]
    assert text.startswith("⚠️ No se pudo imprimir una comanda.\nPor favor, revise la impresora.")
    assert "La comanda se envió a este chat para evitar que se pierda." in text
    # la comanda completa, tal cual iba a imprimirse, con su línea de pago
    assert text.endswith(f"{TICKET}\n\nPago: EFECTIVO\n" + "═" * 20)
    assert job.admin_backup_sent_at is not None


async def test_the_same_order_is_not_sent_to_the_admin_twice(fake_store):
    first = _job(fake_store)
    await _send(first, printed=False)

    retry = _new_job_for(fake_store, first)  # la IA vuelve a imprimir el mismo pedido
    _, notify = await _send(retry, printed=False)

    notify.assert_not_awaited()


async def test_each_new_order_is_backed_up_while_the_printer_keeps_failing(fake_store):
    for _ in range(2):
        job = _job(fake_store)
        _, notify = await _send(job, printed=False)
        notify.assert_awaited_once()


async def test_if_the_admin_could_not_be_reached_a_later_failure_retries(fake_store):
    job = _job(fake_store)
    await _send(job, printed=False, admin_ok=False)
    assert job.admin_backup_sent_at is None

    retry = _new_job_for(fake_store, job)
    _, notify = await _send(retry, printed=False, admin_ok=True)

    notify.assert_awaited_once()


async def test_a_backup_error_never_breaks_the_order_flow(fake_store):
    job = _job(fake_store)

    with patch.object(print_jobs, "PrinterClient") as printer_cls, patch.object(
        print_jobs.HandoffService, "notify_admins", new=AsyncMock(side_effect=RuntimeError("whatsapp caído"))
    ):
        printer_cls.return_value.print_text = AsyncMock(return_value=False)
        printed = await send_print_job(job, "efectivo")

    assert printed is False
    assert fake_store.stores[PrintJobs][job.print_job_id].status == PrintJobStatus.FAILED.value


async def test_business_without_printer_sends_the_order_to_the_admin_chat(fake_store):
    job = _job(fake_store)
    # Tiene impresora, pero eligió recibir las comandas en el chat.
    seed_printer(fake_store, fake_store.stores[Businesses][job.business_id]).orders_to_admin_chat = True

    with patch.object(print_jobs, "PrinterClient") as printer_cls, patch.object(
        print_jobs.HandoffService, "notify_admins", new=AsyncMock(return_value=True)
    ) as notify:
        sent = await send_print_job(job, "efectivo")

    assert sent is True
    printer_cls.assert_not_called()
    assert notify.await_args.args[1] == (
        f"🧾 Nuevo pedido confirmado\n\n{TICKET}\n\nPago: EFECTIVO\n" + "═" * 20
    )
    assert job.status == PrintJobStatus.SENT.value
