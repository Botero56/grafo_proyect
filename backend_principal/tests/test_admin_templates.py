"""Avisos al admin fuera de la ventana de 24h: plantilla por negocio y por
tipo de aviso, con el aviso como parámetro; sin plantilla, un ERROR."""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.config import admin_api_settings
from app.firestore_db.firebase_schemas import Configurations
from app.services import worker_entry
from app.services.conversation import handoff_service
from app.services.conversation.handoff_service import HandoffService, template_param
from app.services.delivery import quotes
from app.services.whatsapp_client.whatsapp_client import WhatsAppClient
from tests.conftest import seed_business, seed_configuration, seed_conversation, seed_customer

TEMPLATES = {
    "comanda_respaldo": {"name": "comanda_respaldo_v1", "language": "es"},
    "cotizacion_domicilio": {"name": "cotizacion_v1", "language": "es"},
}


def test_the_parameter_is_one_line_without_tabs_and_capped():
    text = "🧾 Nuevo pedido\n\nCliente: Ana\n\tDirección:   Cra 1   #2-3\n" + "x" * 2000

    param = template_param(text)

    assert "\n" not in param and "\t" not in param and "    " not in param
    assert param.startswith("🧾 Nuevo pedido · Cliente: Ana · Dirección: Cra 1 #2-3 · ")
    assert len(param) <= 900 and param.endswith("…")


async def test_send_template_puts_the_parameters_in_the_body():
    client = WhatsAppClient(token="t", phone_number_id="1")
    with patch.object(client, "_send", new=AsyncMock(return_value={"ok": True})) as send:
        assert await client.send_template("573001", "cotizacion_v1", "es", ["Calle 5"])

    assert send.await_args.args[0]["template"] == {
        "name": "cotizacion_v1",
        "language": {"code": "es"},
        "components": [{"type": "body", "parameters": [{"type": "text", "text": "Calle 5"}]}],
    }


async def _notify_outside_window(fake_store, kind, templates=TEMPLATES):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, manager_phone="573009999999", admin_templates=templates)
    client = MagicMock()
    client.send_template = AsyncMock(return_value=True)
    client.send_message = AsyncMock(return_value=True)
    with patch.object(handoff_service.WhatsAppClient, "for_business", return_value=client):
        sent = await HandoffService().notify_admins(business, "Línea 1\nLínea 2", kind)
    return sent, client


async def test_each_notice_uses_its_own_template(fake_store):
    sent, client = await _notify_outside_window(fake_store, "comanda_respaldo")

    assert sent is True
    client.send_template.assert_awaited_once_with("573009999999", "comanda_respaldo_v1", "es", ["Línea 1 · Línea 2"])
    client.send_message.assert_not_awaited()


async def test_a_notice_without_its_template_is_an_error_not_a_warning(fake_store, caplog):
    sent, client = await _notify_outside_window(fake_store, "atencion_cliente")

    assert sent is False
    client.send_template.assert_not_awaited()
    record = next(r for r in caplog.records if getattr(r, "fields", {}).get("ops_event") == "admin_notice_undelivered")
    assert record.levelname == "ERROR" and record.fields["notice_kind"] == "atencion_cliente"


async def test_the_delivery_quote_request_uses_the_quote_template(fake_store):
    business = seed_business(fake_store)
    customer = seed_customer(fake_store, business)
    conversation = seed_conversation(fake_store, business, customer.customer_id)

    with patch.object(HandoffService, "notify_admins", new=AsyncMock(return_value=True)) as notify:
        await quotes.request_quote(business, customer, conversation, "Vereda El Salado")

    assert notify.await_args.args[2] == "cotizacion_domicilio"


def test_patch_validates_admin_templates(fake_store):
    business = seed_business(fake_store)
    configuration = seed_configuration(fake_store, business)
    client = TestClient(worker_entry.app)
    headers = {"X-API-Key": admin_api_settings.API_KEY}
    url = f"/api/v1/admin/configurations/{business.bussiness_id}"

    ok = client.patch(url, json={"admin_templates": TEMPLATES}, headers=headers)
    bad_kind = client.patch(url, json={"admin_templates": {"otra_cosa": {"name": "x"}}}, headers=headers)
    bad_name = client.patch(url, json={"admin_templates": {"comanda_respaldo": {"name": "Con Espacios"}}}, headers=headers)

    assert ok.status_code == 200
    assert ok.json()["admin_templates"]["cotizacion_domicilio"] == {"name": "cotizacion_v1", "language": "es"}
    assert fake_store.stores[Configurations][configuration.configuration_id].admin_templates == TEMPLATES
    assert bad_kind.status_code == bad_name.status_code == 422
