"""Cada comprobante que manda el cliente queda guardado en Cloud Storage
({negocio}/{cliente}/{fecha}/...) y su URL en el pedido: si hay un reclamo,
se sabe exactamente qué imagen envió. Guardarlo nunca debe frenar el pago."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app.firestore_db.firebase_schemas import Orders
from app.services.excel_report_service.constants import DEFAULT_TZ
from app.services.payments import receipt_storage, transfer_receipt
from app.services.payments.transfer_receipt import TransferReceiptHandler
from tests.test_payment_flow import _pending_transfer_with_held_ticket, _receipt_message, _whatsapp


def test_the_path_is_business_then_customer_then_date():
    now = datetime(2026, 10, 5, 13, 4, 9, tzinfo=DEFAULT_TZ)

    path = receipt_storage.receipt_path("biz1", "cust1", "ord1", "image/jpeg", now)

    assert path == "biz1/cust1/2026-10-05/ord1_130409.jpg"


def test_the_image_is_uploaded_and_its_url_returned(monkeypatch):
    monkeypatch.setattr(receipt_storage.storage_settings, "RECEIPTS_BUCKET", "comprobantes")
    client = MagicMock()
    with patch.object(receipt_storage, "_client", return_value=client):
        url = receipt_storage.store_receipt("biz1", "cust1", "ord1", b"jpeg", "image/jpeg")

    path = client.bucket.return_value.blob.call_args.args[0]
    assert client.bucket.call_args.args == ("comprobantes",)
    assert path.startswith("biz1/cust1/") and path.endswith(".jpg")
    client.bucket.return_value.blob.return_value.upload_from_string.assert_called_once()
    assert url == f"https://storage.cloud.google.com/comprobantes/{path}"


def test_a_storage_failure_returns_none_instead_of_raising(monkeypatch):
    monkeypatch.setattr(receipt_storage.storage_settings, "RECEIPTS_BUCKET", "comprobantes")
    client = MagicMock()
    client.bucket.return_value.blob.return_value.upload_from_string.side_effect = RuntimeError("403")
    with patch.object(receipt_storage, "_client", return_value=client):
        assert receipt_storage.store_receipt("biz1", "cust1", "ord1", b"jpeg", "image/jpeg") is None


async def test_every_receipt_url_is_kept_on_the_order_even_if_unreadable(fake_store):
    business, customer, order_id, _ = await _pending_transfer_with_held_ticket(fake_store)
    urls = iter(["https://storage/1.jpg", "https://storage/2.jpg"])

    with patch.object(transfer_receipt, "store_receipt", side_effect=lambda *a: next(urls)), patch.object(
        transfer_receipt, "read_transfer_receipt", return_value=None
    ):
        for _ in range(2):  # dos fotos ilegibles
            await TransferReceiptHandler(_whatsapp()).maybe_handle(
                business, customer, _receipt_message(fake_store, business, customer)
            )

    assert fake_store.stores[Orders][order_id].transfer_receipt_urls == [
        "https://storage/1.jpg",
        "https://storage/2.jpg",
    ]


async def test_a_storage_failure_does_not_block_the_payment(fake_store):
    business, customer, order_id, _ = await _pending_transfer_with_held_ticket(fake_store)
    reading = transfer_receipt.TransferReceipt(es_comprobante=True, valor=21000.0)

    with patch.object(transfer_receipt, "store_receipt", return_value=None), patch.object(
        transfer_receipt, "read_transfer_receipt", return_value=reading
    ), patch("app.services.printer_client.print_jobs.PrinterClient") as printer_cls:
        printer_cls.return_value.print_text = AsyncMock(return_value=True)
        await TransferReceiptHandler(_whatsapp()).maybe_handle(
            business, customer, _receipt_message(fake_store, business, customer)
        )

    order = fake_store.stores[Orders][order_id]
    assert order.payment_status == "validado"
    assert not order.transfer_receipt_urls
