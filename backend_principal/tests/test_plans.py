"""Planes comerciales: cada función bloqueada se corta en el backend (tools,
comprobante, domiciliarios, impresora, cierre diario), no solo en el panel."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.ai.ai_restaurant.admin.admin_tools import validar_transferencia
from app.ai.ai_restaurant.role_resolver import resolve_user_role
from app.config import admin_api_settings
from app.firestore_db.firebase_schemas import Businesses, Orders, PrintJobs
from app.schemas.enum_schemas import PaymentStatus, PrintJobStatus
from app.services import plans, worker_entry
from app.services.delivery import tracking
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.payments import transfer_receipt
from app.services.payments.transfer_receipt import TransferReceiptHandler
from app.services.printer_client import print_jobs
from tests.conftest import FakeToolContext, seed_business, seed_configuration
from tests.test_delivery_tracking import CARLOS, NOW, _order, biz  # noqa: F401 (fixture)
from tests.test_panel_proxy import _auth, _token, proxy  # noqa: F401 (fixture)
from tests.test_payment_flow import _pending_transfer_with_held_ticket, _printer, _receipt_message, _whatsapp

client = TestClient(worker_entry.app)
HEADERS = {"X-API-Key": admin_api_settings.API_KEY}


def _set_plan(business: Businesses, plan: str) -> None:
    business.plan = plan
    FireStoreCRUDService().update(business)


def test_a_business_without_plan_keeps_everything():
    assert plans.plan_of(Businesses()) == plans.Plan.PREMIUM
    assert plans.features_of(SimpleNamespace(plan="basico")) == frozenset()
    assert plans.features_of(SimpleNamespace(plan="avanzado")) == {plans.COMPROBANTE_AUTO, plans.IMPRESORA}
    assert plans.required_plan(plans.REPORTES) == plans.Plan.PREMIUM


def test_tools_outside_the_plan_do_not_run(fake_store):
    business = seed_business(fake_store, plan="avanzado")
    ctx = FakeToolContext(business.bussiness_id)

    blocked = plans.gate_tools(SimpleNamespace(name="generate_excel_report"), {}, ctx)
    assert blocked == {"ok": False, "reason": "no_incluido_en_el_plan", "plan_requerido": "premium"}
    assert plans.gate_tools(SimpleNamespace(name="get_today_menu"), {}, ctx) is None

    _set_plan(business, "premium")
    assert plans.gate_tools(SimpleNamespace(name="generate_excel_report"), {}, ctx) is None


async def test_basic_plan_does_not_read_the_receipt_and_asks_the_admin(fake_store):
    business, customer, order_id, job_id = await _pending_transfer_with_held_ticket(fake_store)
    _set_plan(business, "basico")
    whatsapp = _whatsapp()

    with patch.object(transfer_receipt, "read_transfer_receipt") as read, patch.object(
        transfer_receipt.HandoffService, "notify_human", new=AsyncMock(return_value=True)
    ) as notify, patch.object(transfer_receipt, "store_receipt", return_value="gs://comprobante"):
        handled = await TransferReceiptHandler(whatsapp).maybe_handle(
            business, customer, _receipt_message(fake_store, business, customer)
        )

    assert handled is True
    read.assert_not_called()
    order = fake_store.stores[Orders][order_id]
    assert order.payment_status == PaymentStatus.PENDIENTE_VALIDACION.value
    assert order.transfer_receipt_urls == ["gs://comprobante"]  # la evidencia se guarda igual
    assert whatsapp.send_message.await_args.args[1] == transfer_receipt.MANUAL_REVIEW_TEXT
    assert f"pago ok {customer.phone_number}" in notify.await_args.kwargs["detalle"]
    assert fake_store.stores[PrintJobs][job_id].status == PrintJobStatus.PENDING.value


def _admin_decision_mocks():
    whatsapp = patch.object(transfer_receipt, "WhatsAppClient")
    note = patch.object(transfer_receipt.shared_agent_runner(), "append_human_message", new=AsyncMock())
    admin_chat = patch.object(print_jobs.HandoffService, "notify_admins", new=AsyncMock(return_value=True))
    return whatsapp, note, admin_chat


async def test_admin_approves_and_without_printer_in_the_plan_the_ticket_goes_to_the_admin_chat(fake_store):
    business, customer, order_id, job_id = await _pending_transfer_with_held_ticket(fake_store)
    _set_plan(business, "basico")  # tiene impresora configurada, pero no en su plan
    whatsapp, note, admin_chat = _admin_decision_mocks()

    with whatsapp as client_cls, note as add_note, admin_chat as notify, _printer() as printer_cls:
        client_cls.for_business.return_value.send_message = AsyncMock(return_value=True)
        result = await validar_transferencia(FakeToolContext(business.bussiness_id), customer.phone_number, True)

    assert result["ok"] is True and result["customer_notified"] is True
    assert fake_store.stores[Orders][order_id].payment_status == PaymentStatus.VALIDADO.value
    printer_cls.assert_not_called()
    assert "Nuevo pedido confirmado" in notify.await_args.args[1]
    assert fake_store.stores[PrintJobs][job_id].status == PrintJobStatus.SENT.value
    sent = client_cls.for_business.return_value.send_message.await_args.args[1]
    assert sent == transfer_receipt.APPROVED_TEXT
    add_note.assert_awaited_once()  # la IA del cliente sabe la decisión


async def test_admin_rejects_and_the_order_waits_for_a_receipt_again(fake_store):
    business, customer, order_id, job_id = await _pending_transfer_with_held_ticket(fake_store)
    whatsapp, note, admin_chat = _admin_decision_mocks()

    with whatsapp as client_cls, note, admin_chat:
        client_cls.for_business.return_value.send_message = AsyncMock(return_value=True)
        result = await validar_transferencia(FakeToolContext(business.bussiness_id), customer.phone_number, False)

    assert result["ok"] is True and result["aprobado"] is False
    assert fake_store.stores[Orders][order_id].payment_status == PaymentStatus.PENDIENTE_COMPROBANTE.value
    assert fake_store.stores[PrintJobs][job_id].status == PrintJobStatus.PENDING.value
    assert client_cls.for_business.return_value.send_message.await_args.args[1] == transfer_receipt.REJECTED_TEXT


async def test_validating_without_a_pending_transfer_says_so(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    from tests.conftest import seed_customer

    customer = seed_customer(fake_store, business, name="Ana")
    result = await validar_transferencia(FakeToolContext(business.bussiness_id), customer.phone_number, True)
    assert result == {"ok": False, "reason": "sin_transferencia_por_validar"}


def test_couriers_only_get_their_bot_in_premium(fake_store):
    business = seed_business(fake_store, plan="avanzado")
    seed_configuration(fake_store, business, couriers=[{"name": "Carlos", "phone": CARLOS}])

    assert resolve_user_role(business.bussiness_id, CARLOS)["role"] == "cliente"
    _set_plan(business, "premium")
    assert resolve_user_role(business.bussiness_id, CARLOS)["role"] == "domiciliario"


async def test_no_daily_close_without_couriers_in_the_plan(fake_store, biz):  # noqa: F811
    business, _ = biz
    _set_plan(business, "avanzado")
    _order(business)
    after_cut = NOW.replace(hour=2) + timedelta(days=1)

    with patch.object(tracking.HandoffService, "notify_admins", AsyncMock(return_value=True)) as notify:
        assert (await tracking.sweep(after_cut))["closed"] == []
    notify.assert_not_awaited()


def test_the_plan_is_changed_by_the_admin_api_and_shown_with_the_business(fake_store):
    business = seed_business(fake_store)
    path = f"/api/v1/admin/businesses/{business.bussiness_id}"

    assert client.get(path, headers=HEADERS).json()["plan"] == "premium"
    response = client.put(f"{path}/plan", json={"plan": "basico", "actor": "samuel@3b-technology.com"}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["features"] == [] and response.json()["changed_by"] == "samuel@3b-technology.com"

    shown = client.get(path, headers=HEADERS).json()
    assert (shown["plan"], shown["features"]) == ("basico", [])
    assert client.put(f"{path}/plan", json={"plan": "gratis", "actor": "x"}, headers=HEADERS).status_code == 422


def test_the_plan_change_is_signed_with_the_verified_email(proxy):  # noqa: F811
    panel, send = proxy
    panel.put(
        "/api/v1/admin/businesses/b1/plan",
        json={"plan": "premium", "actor": "quien-sea"},
        headers=_auth(_token(email="samuel@3b-technology.com")),
    )
    assert json.loads(send.await_args.args[3])["actor"] == "samuel@3b-technology.com"
