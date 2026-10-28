"""Envío de una comanda (PrintJobs) a la impresora y registro del resultado.

Lo usan print_order_ticket (pedido en efectivo, o transferencia ya validada)
y la validación del comprobante (comanda retenida hasta que el pago se
valida, ver app/services/payments/transfer_receipt.py).

Si la impresora falla (apagada, desconectada, timeout, error del túnel...),
la comanda se manda al chat de administración para que no se pierda. Una
sola vez por pedido: si ya se le mandó al admin, no se repite.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from starlette.concurrency import run_in_threadpool

from app.services.phones import contact_phone
from app.config import printer_settings
from app.services import plans, secrets
from app.firestore_db.firebase_schemas import Businesses, Configurations, Customers, Orders, PrintJobs
from app.schemas.enum_schemas import PaymentMethod, PrintJobStatus
from app.services.conversation.handoff_service import COMANDA_RESPALDO, HandoffService
from app.services.daily_menu.menu_diff import format_cop, format_fee
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services.printer_client.printer_client import PrinterClient

logger = logging.getLogger("whatsapp-webhook")

_THICK = "═" * 20
_THIN = "─" * 20


def _phone(number: str) -> str:
    """573001234567 -> 300 123 4567; cualquier otro formato sale tal cual."""
    digits = "".join(c for c in number if c.isdigit())
    if len(digits) == 12 and digits.startswith("57"):
        digits = digits[2:]
    return f"{digits[:3]} {digits[3:6]} {digits[6:]}" if len(digits) == 10 else number


def _ticket_name(business: Optional[Businesses]) -> str:
    """Nombre del encabezado de la comanda: Configurations.restaurant_name
    (el nombre comercial que el negocio quiere ver) o, sin él, el de
    Businesses."""
    if business is None:
        return ""
    configuration = TenantScopedQuery.get_one_for_business(Configurations, business.bussiness_id)
    return (configuration.restaurant_name if configuration else None) or business.name or ""


def build_ticket(
    business: Optional[Businesses], customer: Optional[Customers], order: Orders, items_text: str
) -> str:
    """Comanda para cocina/domicilio, sin la línea de pago (la agrega
    send_print_job al imprimir). `items_text` es la sección PEDIDO que arma
    la IA; todo lo demás sale del pedido guardado. Un dato vacío no imprime
    su línea."""
    sections = [
        [_THICK, _ticket_name(business).upper(), "COMANDA", _THICK],
        # shortcut: el puente recibe texto plano; "grande" es este bloque. Letra
        # doble requiere que el puente de impresión soporte formato.
        [_THIN, f">>>   # {int(order.daily_code)}   <<<", _THIN] if order.daily_code else None,
        [f"PEDIDO #{order.order_id[-6:].upper()}"],
        [
            f"Cliente: {customer.name}" if customer and customer.name else None,
            f"Tel: {_phone(contact_phone(customer))}" if customer and contact_phone(customer) else None,
        ],
        [
            f"Dirección: {order.delivery_address}" if order.delivery_address else None,
            f"Barrio: {order.delivery_zone}" if order.delivery_zone else None,
        ],
        ["PEDIDO", items_text.strip()],
        [
            _THIN,
            f"Domicilio: {format_fee(order.delivery_fee, '')}" if order.delivery_fee is not None else None,
            f"Total: {format_cop(order.order_total, '')}" if order.order_total is not None else None,
        ],
    ]
    blocks = ["\n".join(line for line in section if line) for section in sections if section]
    return "\n\n".join(block for block in blocks if block)


def _with_payment(content: str, payment_method: Optional[str], cash_due: Optional[float] = None) -> str:
    """send_print_job solo se llama cuando la comanda ya puede salir: con
    transferencia eso significa que el comprobante ya se validó.

    `cash_due`: pago mixto -- la transferencia no alcanzó y el cliente paga
    el resto en efectivo al recibir. Va en su propia línea para que el
    domiciliario sepa cuánto cobrar."""
    if not payment_method:
        return f"{content}\n{_THICK}"
    label = payment_method.upper()
    if payment_method == PaymentMethod.TRANSFERENCIA.value:
        label += " ✓"
    elif payment_method == PaymentMethod.ACUERDO.value:
        # La administradora confirmó que no se cobra: que el domiciliario lo vea.
        label = "NO COBRAR (acuerdo con la administración)"
    cash_line = f"\nCobrar en efectivo: {format_cop(cash_due, '')}" if cash_due else ""
    return f"{content}\n\nPago: {label}{cash_line}\n{_THICK}"


def _printer_for(business_id: str, configuration: Optional[Configurations]):
    """Impresora del negocio, según su Configurations.printer_settings:

    - PrinterClient: impresora habilitada, con URL y token legible.
    - False: habilitada pero sin token legible -- falla cerrado (se trata
      como impresora caída: la comanda va al admin), nunca se manda sin
      autenticar.
    - None: el negocio no tiene impresora; la comanda va al chat del admin.

    La URL sale SIEMPRE de la configuración de `business_id`: la comanda de
    un negocio no puede llegar a la impresora de otro."""
    settings = (configuration.printer_settings if configuration is not None else None) or {}
    if settings.get("enabled") and settings.get("endpoint_url"):
        token = secrets.get_secret_or_none(settings.get("auth_secret_ref"))
        if not token:
            logger.error("La impresora del negocio %s no tiene token legible: la comanda va al admin", business_id)
            return False
        return PrinterClient(
            settings["endpoint_url"],
            token=token,
            business_id=business_id,
            timeout=settings.get("timeout_seconds") or 15,
            copies=settings.get("copies") or 1,
            paper_width=settings.get("paper_width"),
        )
    # shortcut: transición del PRINT_URL global, solo para el negocio que ya
    # lo usaba y sin token; se quita cuando ese negocio tenga printer_settings.
    if printer_settings.LEGACY_PRINT_URL and business_id == printer_settings.LEGACY_PRINT_BUSINESS_ID:
        logger.warning(
            "El negocio %s imprime por LEGACY_PRINT_URL (sin token): configúralo con "
            "PUT /api/v1/admin/configurations/%s/printer",
            business_id,
            business_id,
        )
        return PrinterClient(printer_settings.LEGACY_PRINT_URL, business_id=business_id)
    return None


async def send_print_job(
    print_job: PrintJobs, payment_method: Optional[str], cash_due: Optional[float] = None
) -> bool:
    print_job.attempts = (print_job.attempts or 0) + 1
    print_job.sent_at = datetime.now(timezone.utc)

    text = _with_payment(print_job.content or "", payment_method, cash_due)

    configuration = await run_in_threadpool(
        TenantScopedQuery.get_one_for_business, Configurations, print_job.business_id
    )
    printer = None
    # Sin impresora en el plan, la comanda va al chat del admin aunque haya
    # una configurada (ver plans.py).
    if (configuration is None or not configuration.orders_to_admin_chat) and await run_in_threadpool(
        plans.allows, print_job.business_id, plans.IMPRESORA
    ):
        printer = await run_in_threadpool(_printer_for, print_job.business_id, configuration)
    if printer is None:
        return await _send_order_to_admin_chat(print_job, text)

    success = bool(printer) and await printer.print_text(text, payment_method)

    if success:
        print_job.status = PrintJobStatus.PRINTED.value
        print_job.printed_at = datetime.now(timezone.utc)
    else:
        print_job.status = PrintJobStatus.FAILED.value
        print_job.failed_at = datetime.now(timezone.utc)
        print_job.error = "No se pudo contactar la impresora." if printer else "La impresora no tiene token configurado."

    FireStoreCRUDService().update(print_job)

    if not success:
        try:
            await _send_to_admin_chat(print_job, text)
        except Exception:
            # El respaldo nunca debe romper el flujo del pedido: la comanda
            # sigue guardada en PrintJobs aunque el aviso no salga.
            logger.exception("No se pudo mandar al admin la comanda %s", print_job.print_job_id)
    return success


async def _send_order_to_admin_chat(print_job: PrintJobs, text: str) -> bool:
    """Negocio sin impresora (o con Configurations.orders_to_admin_chat): la
    comanda va al chat de administración en vez de a cocina."""
    business = await run_in_threadpool(Businesses.collection.get, print_job.business_id)
    try:
        sent = business is not None and await HandoffService().notify_admins(
            business, f"🧾 Nuevo pedido confirmado\n\n{text}", COMANDA_RESPALDO
        )
    except Exception:
        # Como el respaldo: nunca rompe el pedido; queda FAILED en PrintJobs.
        logger.exception("No se pudo mandar al admin la comanda %s", print_job.print_job_id)
        sent = False
    now = datetime.now(timezone.utc)
    if sent:
        print_job.status = PrintJobStatus.SENT.value
        print_job.admin_backup_sent_at = now
    else:
        print_job.status = PrintJobStatus.FAILED.value
        print_job.failed_at = now
        print_job.error = "No se pudo mandar la comanda al chat de administración."
    await run_in_threadpool(FireStoreCRUDService().update, print_job)
    return bool(sent)


async def release_held_ticket(order: Orders) -> None:
    """Manda a cocina la comanda que print_order_ticket dejó retenida
    esperando el pago (comprobante o autorización de la administradora). Si
    todavía no existe (la IA aún no la armó), print_order_ticket la imprimirá
    directo, porque el pago ya figura resuelto."""
    held = await run_in_threadpool(
        lambda: PrintJobs.collection.filter(
            order_id=order.order_id, status=PrintJobStatus.PENDING.value
        ).get()
    )
    if held is not None:
        await send_print_job(held, order.payment_method, order.cash_due)


async def _send_to_admin_chat(print_job: PrintJobs, ticket: str) -> None:
    already_sent = [
        job
        for job in PrintJobs.collection.filter(order_id=print_job.order_id).fetch()
        if job.admin_backup_sent_at is not None
    ]
    if already_sent:
        return

    business = Businesses.collection.get(print_job.business_id)
    if business is None:
        return

    # La comanda ya trae cliente, teléfono y pago: va completa, sin repetirlos.
    lines = [
        "⚠️ No se pudo imprimir una comanda.",
        "Por favor, revise la impresora.",
        "La comanda se envió a este chat para evitar que se pierda.",
        "",
        ticket,
    ]

    if await HandoffService().notify_admins(business, "\n".join(lines), COMANDA_RESPALDO):
        print_job.admin_backup_sent_at = datetime.now(timezone.utc)
        FireStoreCRUDService().update(print_job)
