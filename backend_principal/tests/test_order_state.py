"""El pedido como estado: notas que llegan a la comanda, correcciones que
no dependen de la memoria del modelo y confirmación desde el estado.

Casos reales: "unos frijoles bien caldudos" salía en la comanda como
"frijoles" sin la nota; "no, espera" no siempre corregía el pedido."""

from unittest.mock import AsyncMock, patch

from app.ai.ai_restaurant.user.tools import customer_tools
from app.ai.ai_restaurant.user.tools.order_tools import confirm_order, render_order_state, update_order
from app.firestore_db.firebase_schemas import Customers, Orders, PrintJobs
from app.schemas.enum_schemas import PaymentStatus
from app.services.delivery import quotes
from tests.conftest import (
    FakeToolContext,
    seed_active_menu,
    seed_business,
    seed_configuration,
    seed_conversation,
    seed_customer,
    seed_printer,
)

ZONES = [{"nombre": "Cerro Azul", "tarifa": 3000, "alias": []}]
MENU = {"accion": "agregar", "producto": "menu_del_dia", "elecciones": {"Sopa": "Frijoles"}, "notas": "frijoles bien caldudos"}


CUENTA = {"banco": "Nequi", "tipo": "billetera", "numero": "3001234567", "titular": "Edy Gourmet SAS", "llave": None}


def _ctx(fake_store, **customer_fields):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business, delivery_zones=ZONES, cuentas_transferencia=[CUENTA])
    seed_printer(fake_store, business)
    seed_active_menu(fake_store, business)  # menú del día $18.500, sopa ajiaco/frijoles
    customer = seed_customer(fake_store, business, **customer_fields)
    conversation = seed_conversation(fake_store, business, customer.customer_id)
    return FakeToolContext(business.bussiness_id, customer.customer_id, conversation.conversation_id)


async def _ready_order(fake_store, forma="efectivo"):
    ctx = _ctx(fake_store, name="Ana")
    await update_order(
        [MENU, {"accion": "entrega", "direccion": "Calle 10 # 20-30, Cerro Azul torre 3", "barrio": "Cerro Azul", "referencias": "portería 2"},
         {"accion": "pago", "forma": forma}],
        ctx,
    )
    return ctx


async def test_a_product_note_is_kept_in_the_state(fake_store):
    ctx = _ctx(fake_store)

    result = await update_order([MENU], ctx)

    assert result["ok"]
    item = ctx.state["pedido"]["items"][0]
    assert (item["id"], item["notas"], item["subtotal"]) == ("i1", "frijoles bien caldudos", 18500)
    assert 'nota: "frijoles bien caldudos"' in render_order_state(ctx.state)


async def test_corrections_and_undo_act_on_the_state(fake_store):
    ctx = _ctx(fake_store)
    await update_order([MENU], ctx)
    await update_order([{"accion": "agregar", "producto": "otras_opciones", "proteina": "Res", "elecciones": {"Sopa": "Ajiaco"}}], ctx)

    # "no, espera, no es correcto": deshace el último cambio.
    await update_order([{"accion": "deshacer"}], ctx)
    assert [i["id"] for i in ctx.state["pedido"]["items"]] == ["i1"]

    await update_order([{"accion": "modificar", "item_id": "i1", "cantidad": 2}], ctx)
    assert ctx.state["pedido"]["items"][0]["qty"] == 2
    assert ctx.state["pedido"]["items"][0]["notas"] == "frijoles bien caldudos"
    assert ctx.state["pedido"]["total"] == 37000

    await update_order([{"accion": "quitar", "item_id": "i1"}], ctx)
    assert ctx.state["pedido"]["items"] == []


async def test_invalid_items_are_rejected_without_losing_the_rest(fake_store):
    ctx = _ctx(fake_store)

    result = await update_order(
        [MENU, {"accion": "agregar", "producto": "otras_opciones", "proteina": "Langosta", "elecciones": {"Sopa": "Ajiaco"}}],
        ctx,
    )

    assert not result["ok"]
    assert "Langosta" in result["errores"][0]
    assert len(ctx.state["pedido"]["items"]) == 1


async def test_saved_address_is_shown_to_confirm_not_assumed(fake_store):
    ctx = _ctx(fake_store, name="Ana", address="Cerro Azul torre 3")

    await update_order([MENU], ctx)

    order = ctx.state["pedido"]
    assert order["direccion"] is None and order["nombre"] == "Ana"
    assert "Dirección guardada (confírmala con el cliente): Cerro Azul torre 3" in render_order_state(ctx.state)


async def test_unknown_zone_asks_the_team_for_a_quote_automatically(fake_store):
    ctx = _ctx(fake_store)

    with patch.object(quotes, "request_quote", new=AsyncMock(return_value=True)) as request:
        result = await update_order([MENU, {"accion": "entrega", "direccion": "Vereda El Salado", "barrio": "El Salado"}], ctx)

    request.assert_awaited_once()
    assert ctx.state["pedido"]["domicilio"] is None
    assert any("domicilio" in f for f in result["falta"])


async def test_confirm_needs_everything_first(fake_store):
    ctx = _ctx(fake_store)
    await update_order([MENU], ctx)

    result = await confirm_order(ctx)

    assert result == {"ok": False, "falta": ["dirección de entrega", "forma de pago"]}
    assert fake_store.stores[Orders] == {}


async def test_confirming_ends_the_courier_customer_mode(fake_store):
    """El domiciliario vuelve a su agente apenas confirma su pedido (prueba
    real 2026-10-16: reportó una entrega y lo atendió el agente de clientes)."""
    ctx = await _ready_order(fake_store)
    customer = Customers.collection.get(ctx.state["customer_id"])
    customer.courier_as_customer_date = "2026-10-16"

    with patch("app.services.printer_client.print_jobs.PrinterClient") as printer_cls:
        printer_cls.return_value.print_text = AsyncMock(return_value=True)
        assert (await confirm_order(ctx))["ok"]

    assert customer.courier_as_customer_date is None


async def test_confirm_saves_and_prints_the_ticket_from_the_state_with_every_note(fake_store):
    ctx = await _ready_order(fake_store)

    with patch("app.services.printer_client.print_jobs.PrinterClient") as printer_cls:
        printer_cls.return_value.print_text = AsyncMock(return_value=True)
        result = await confirm_order(ctx)

    assert result["ok"] and result["comanda"] == "impresa" and result["total"] == 21500
    order = fake_store.stores[Orders][result["order_id"]]
    assert "Nota: frijoles bien caldudos" in order.notes
    assert order.delivery_address == "Calle 10 # 20-30, Cerro Azul torre 3 (portería 2)"
    ticket = printer_cls.return_value.print_text.await_args.args[0]
    assert "1x Pollo al curry con champiñones\nSopa: Frijoles\nNota: frijoles bien caldudos" in ticket
    assert "Referencias: portería 2" in ticket

    # Un segundo "sí" (o un reintento) no duplica el pedido.
    again = await confirm_order(ctx)
    assert again["ya_confirmado"] and len(fake_store.stores[Orders]) == 1

    # "No, espera" después de confirmar no reabre el borrador para confirmarlo otra vez.
    undo = await update_order([{"accion": "deshacer"}], ctx)
    assert undo["ok"] is False and ctx.state["pedido"]["estado"] == "confirmado"
    await confirm_order(ctx)
    assert len(fake_store.stores[Orders]) == 1


async def test_transfer_returns_the_exact_amount_and_holds_the_ticket(fake_store):
    ctx = await _ready_order(fake_store, forma="transferencia")

    result = await confirm_order(ctx)

    assert result["monto_a_transferir"] == 21500
    # La cuenta sale de la configuración, tal cual: la IA solo la copia.
    assert result["cuentas_transferencia"] == [CUENTA]
    assert result["comanda"] == "en_espera"
    assert len(fake_store.stores[PrintJobs]) == 1


async def test_payment_after_an_agreement_goes_to_the_confirmed_order(fake_store):
    ctx = await _ready_order(fake_store, forma="acuerdo")
    with patch("app.services.payments.payment_agreement.request_authorization", new=AsyncMock(return_value=True)):
        confirmed = await confirm_order(ctx)
    order = fake_store.stores[Orders][confirmed["order_id"]]
    order.payment_status = PaymentStatus.POR_COBRAR.value  # la administración decidió cobrar

    result = await update_order([{"accion": "pago", "forma": "transferencia"}], ctx)

    assert result["monto_a_transferir"] == 21500
    assert result["cuentas_transferencia"] == [CUENTA]
    assert order.payment_method == "transferencia"
    assert len(fake_store.stores[Orders]) == 1


async def test_a_new_order_after_confirming_keeps_who_and_where(fake_store):
    ctx = await _ready_order(fake_store)
    with patch("app.services.printer_client.print_jobs.PrinterClient") as printer_cls:
        printer_cls.return_value.print_text = AsyncMock(return_value=True)
        await confirm_order(ctx)

    await update_order([{"accion": "agregar", "producto": "menu_del_dia", "elecciones": {"Sopa": "Ajiaco"}}], ctx)

    order = ctx.state["pedido"]
    assert order["estado"] == "armando" and len(order["items"]) == 1
    assert order["nombre"] == "Ana" and order["direccion_guardada"] == "Calle 10 # 20-30, Cerro Azul torre 3"


async def test_username_customer_cannot_confirm_without_a_phone(fake_store):
    """Prueba real 2026-10-20: cliente con usuario de WhatsApp (sin número)
    confirmó y la comanda salió sin teléfono para el domiciliario."""
    ctx = _ctx(fake_store, name="Sebastián", phone="CO.1780159176423859")
    await update_order([MENU, {"accion": "entrega", "direccion": "Calle 10 # 20-30, Cerro Azul torre 3", "barrio": "Cerro Azul"}, {"accion": "pago", "forma": "efectivo"}], ctx)

    blocked = await confirm_order(ctx)
    assert blocked["ok"] is False and "celular" in blocked["falta"][0]
    assert fake_store.stores[Orders] == {}

    bad = await update_order([{"accion": "telefono", "numero": "12345"}], ctx)
    assert bad["errores"] and ctx.state["pedido"]["telefono"] is None

    await update_order([{"accion": "telefono", "numero": "300 123 4567"}], ctx)
    customer = Customers.collection.get(ctx.state["customer_id"])
    assert customer.contact_phone == "573001234567"
    assert "Celular: 573001234567" in render_order_state(ctx.state)

    with patch("app.services.printer_client.print_jobs.PrinterClient") as printer_cls:
        printer_cls.return_value.print_text = AsyncMock(return_value=True)
        assert (await confirm_order(ctx))["ok"]
    assert "Tel: 300 123 4567" in printer_cls.return_value.print_text.await_args.args[0]

    # El próximo pedido ya no lo vuelve a pedir.
    await update_order([MENU], ctx)
    assert ctx.state["pedido"]["telefono"] == "573001234567"


async def test_a_phone_customer_is_never_asked_for_a_phone(fake_store):
    ctx = await _ready_order(fake_store)
    assert not any("celular" in f for f in (await update_order([], ctx))["falta"])


async def test_without_barrio_the_fee_waits_and_no_quote_is_requested(fake_store):
    ctx = _ctx(fake_store)

    with patch.object(quotes, "request_quote", new=AsyncMock(return_value=True)) as request:
        result = await update_order([MENU, {"accion": "entrega", "direccion": "Calle 10 # 20-30"}], ctx)
        request.assert_not_awaited()
        assert any("barrio" in f for f in result["falta"])

        result = await update_order([{"accion": "entrega", "barrio": "Cerro Azul"}], ctx)

    assert ctx.state["pedido"]["domicilio"] == 3000
    assert "Calle 10 # 20-30, barrio Cerro Azul" in render_order_state(ctx.state)


async def test_the_barrio_wins_over_a_unit_named_like_another_zone(fake_store):
    ctx = _ctx(fake_store)
    zones = [*ZONES, {"nombre": "La Ceiba", "tarifa": 5000, "alias": []}]

    with patch.object(customer_tools, "zones_for", return_value=zones):
        await update_order([MENU, {"accion": "entrega", "direccion": "Calle 10 # 20-30, Urbanización La Ceiba", "barrio": "Cerro Azul"}], ctx)

    assert ctx.state["pedido"]["domicilio"] == 3000


def test_valid_address_needs_street_numbering_or_to_be_rural():
    from app.ai.ai_restaurant.user.tools.order_tools import valid_address

    for ok in ["Calle 10 # 20-30", "Cra 43A Bis # 12 Sur - 45, Conjunto Los Pinos, Torre 2, Apto 301",
               "Avenida 36A # 45-190", "cl 35 no 53-26", "Diagonal 75B N° 2-15 apto 201", "Vereda El Salado finca 3",
               "Carrera 59c #-40-16", "carrera 59 C número 40 16", "cerro claro torre 2 apto 2004",
               "Cerro Azul torre 3 apto 402", "unidad residencial plazuela apto 450", "Edificio Roterdam apto 2106"]:
        assert valid_address(ok), ok
    for bad in ["Mi casa", "donde siempre", "Laureles", "Calle 10", "Torre 3 apto 402", "", "Carrera 58h #394",
                "Choza Marco Fidel", "Roterdam 2106", "Uniminuto sede Bello"]:
        assert not valid_address(bad), bad


async def test_an_address_without_numbering_is_rejected_and_not_quoted(fake_store):
    # Real 2026-10-21: "Mi casa" llegó a la comanda y el admin la cotizó.
    ctx = _ctx(fake_store)

    with patch.object(quotes, "request_quote", new=AsyncMock(return_value=True)) as request:
        result = await update_order([MENU, {"accion": "entrega", "direccion": "Mi casa", "barrio": "Cerro Azul"}], ctx)

    request.assert_not_awaited()
    assert ctx.state["pedido"]["direccion"] is None
    assert any("Dirección incompleta" in e for e in result["errores"])
