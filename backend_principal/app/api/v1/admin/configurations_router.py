from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from starlette.concurrency import run_in_threadpool

from app.schemas.api.device_schemas import DeviceEventResponse, DeviceResponse
from app.services.devices import devices
from app.api.v1.deps import require_api_key
from app.firestore_db.firebase_schemas import Configurations
from app.schemas.api.configuration_schemas import (
    AdminPhoneNumbersUpdateRequest,
    ConfigurationResponse,
    ConfigurationUpdateRequest,
    CouriersUpdateRequest,
    PrinterSettingsRequest,
    PrinterSettingsResponse,
    PrinterWifiRequest,
)
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery
from app.services import secrets, tenant_profile
from app.services.excel_report_service.constants import DEFAULT_TZ
from app.services.phones import normalize_phone

router = APIRouter(dependencies=[Depends(require_api_key)])


def _get_configuration_or_404(business_id: str) -> Configurations:
    configuration = TenantScopedQuery.get_one_for_business(Configurations, business_id)
    if configuration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Configuración no encontrada para ese negocio",
        )
    return configuration


def _normalized_or_400(phone: str) -> str:
    normalized = normalize_phone(phone)
    if len(normalized) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Teléfono inválido: {phone!r}")
    return normalized


@router.get("/{business_id}", response_model=ConfigurationResponse)
def get_configuration(business_id: str) -> ConfigurationResponse:
    configuration = _get_configuration_or_404(business_id)
    return ConfigurationResponse(**configuration.to_dict())


@router.patch("/{business_id}", response_model=ConfigurationResponse)
def update_configuration(
    business_id: str, payload: ConfigurationUpdateRequest
) -> ConfigurationResponse:
    configuration = _get_configuration_or_404(business_id)

    # mode="json": fechas como texto (Firestore no guarda `date`).
    updates = payload.model_dump(exclude_unset=True, mode="json")
    if updates.get("manager_phone"):
        updates["manager_phone"] = _normalized_or_400(updates["manager_phone"])
    if {"metodos_pago", "cuentas_transferencia"} & updates.keys():
        methods = updates.get("metodos_pago", configuration.metodos_pago) or tenant_profile.DEFAULT_PAYMENT_METHODS
        accounts = updates.get("cuentas_transferencia", configuration.cuentas_transferencia)
        if "transferencia" in methods and not accounts:
            # Sin cuenta el bot le pediría al cliente un pago sin decirle a dónde.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Para aceptar transferencia hace falta al menos una cuenta (cuentas_transferencia)",
            )
    for field, value in updates.items():
        setattr(configuration, field, value)

    FireStoreCRUDService().update(configuration)
    tenant_profile.invalidate(business_id)
    return ConfigurationResponse(**configuration.to_dict())


@router.put("/{business_id}/admin-phone-numbers", response_model=ConfigurationResponse)
def update_admin_phone_numbers(
    business_id: str, payload: AdminPhoneNumbersUpdateRequest
) -> ConfigurationResponse:
    configuration = _get_configuration_or_404(business_id)

    configuration.admin_phone_numbers = list(dict.fromkeys(_normalized_or_400(p) for p in payload.phone_numbers))
    FireStoreCRUDService().update(configuration)
    return ConfigurationResponse(**configuration.to_dict())


@router.put("/{business_id}/couriers", response_model=ConfigurationResponse)
def update_couriers(business_id: str, payload: CouriersUpdateRequest) -> ConfigurationResponse:
    """Reemplaza la lista de domiciliarios. Un teléfono de la lista recibe
    el rol domiciliario (role_resolver): por eso va por acá y no por el
    PATCH, igual que admin-phone-numbers."""
    configuration = _get_configuration_or_404(business_id)
    admins = {normalize_phone(p) for p in configuration.admin_phone_numbers or []}
    previous = {normalize_phone(c.get("phone")): c for c in configuration.couriers or []}
    couriers: dict[str, dict] = {}
    for courier in payload.couriers:
        phone = _normalized_or_400(courier.phone)
        if phone in admins:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{phone} es administrador: no puede ser domiciliario")
        added_at = (previous.get(phone) or {}).get("added_at") or datetime.now(DEFAULT_TZ).isoformat()
        couriers[phone] = {"phone": phone, "name": courier.name.strip(), "added_at": added_at}
    configuration.couriers = list(couriers.values())
    FireStoreCRUDService().update(configuration)
    return ConfigurationResponse(**configuration.to_dict())


def _printer_response(configuration: Configurations) -> PrinterSettingsResponse:
    settings = dict(configuration.printer_settings or {})
    return PrinterSettingsResponse(
        enabled=bool(settings.get("enabled")),
        endpoint_url=settings.get("endpoint_url"),
        token_configured=bool(settings.get("auth_secret_ref")),
        paper_width=settings.get("paper_width"),
        copies=settings.get("copies") or 1,
        timeout_seconds=settings.get("timeout_seconds") or 15,
    )


@router.get("/{business_id}/printer", response_model=PrinterSettingsResponse)
def get_printer(business_id: str) -> PrinterSettingsResponse:
    """El token nunca sale: solo `token_configured`."""
    return _printer_response(_get_configuration_or_404(business_id))


@router.put("/{business_id}/printer", response_model=PrinterSettingsResponse)
async def put_printer(business_id: str, payload: PrinterSettingsRequest) -> PrinterSettingsResponse:
    """Configura la impresora del negocio. El token va a Secret Manager
    (`printer-token--{businessId}`); mandar uno nuevo lo rota."""
    url = (payload.endpoint_url or "").strip() or None
    if url:
        parsed = urlparse(url)
        # https obligatorio: el token viaja en el header.
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="endpoint_url debe ser https://host/... sin usuario ni clave")
    token = (payload.token or "").strip()
    if payload.token is not None and len(token) < 16:
        # El detalle no repite el valor.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El token debe tener al menos 16 caracteres")

    configuration = await run_in_threadpool(_get_configuration_or_404, business_id)
    settings = dict(configuration.printer_settings or {})
    if payload.enabled and not (url and (token or settings.get("auth_secret_ref"))):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Para habilitarla hacen falta endpoint_url y token")

    if token:
        settings["auth_secret_ref"] = await run_in_threadpool(secrets.put_secret, secrets.PRINTER_TOKEN, business_id, token)
    settings.update(
        enabled=payload.enabled,
        endpoint_url=url,
        paper_width=payload.paper_width,
        copies=payload.copies,
        timeout_seconds=payload.timeout_seconds,
    )
    configuration.printer_settings = settings
    await run_in_threadpool(FireStoreCRUDService().update, configuration)
    return _printer_response(configuration)


# --- Raspberry del negocio: versión y actualización ---------------------------
# Usa la URL y el token ya guardados: el token nunca pasa por el navegador.

# El proxy del panel corta a los 30 s; la actualización tiene que contestar antes.
_UPDATE_TIMEOUT_SECONDS = 25


def _printer_target(business_id: str) -> tuple[str, dict[str, str]]:
    settings = dict(_get_configuration_or_404(business_id).printer_settings or {})
    url = settings.get("endpoint_url")
    token = secrets.get_secret_or_none(settings.get("auth_secret_ref"))
    if not url or not token:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La impresora no tiene URL y token configurados")
    # Mismo contrato que las comandas (RUNBOOK §4).
    return url, {"Authorization": f"Bearer {token}", "X-Business-Id": business_id}


async def _call_raspberry(business_id: str, method: str, path: str, timeout: float, json: dict | None = None) -> dict:
    url, headers = await run_in_threadpool(_printer_target, business_id)
    # .../print -> .../version: mismo host que endpoint_url. Sin seguir
    # redirecciones (default de httpx): el token no viaja a otro host.
    target = urljoin(url, path)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, target, headers=headers, json=json)
    except httpx.TimeoutException:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="La Raspberry no respondió a tiempo")
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="No se pudo contactar a la Raspberry")
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.is_error or not isinstance(body, dict):
        # 502 y no el código de la Raspberry: un 401 suyo no es una sesión vencida del panel.
        detail = str((body or {}).get("detail", ""))[:300] if isinstance(body, dict) else ""
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"La Raspberry respondió {response.status_code}" + (f": {detail}" if detail else ""),
        )
    return body


@router.get("/{business_id}/printer/device")
async def get_printer_device(business_id: str) -> dict:
    """Versión del código que corre en la Raspberry del negocio."""
    return await _call_raspberry(business_id, "GET", "version", timeout=8)


@router.post("/{business_id}/printer/update")
async def update_printer_device(business_id: str) -> dict:
    """Le pide a la Raspberry que baje el código nuevo de GitLab y se reinicie.
    Devuelve `{"updated": false, "version"}` o `{"updated": true, "from", "to", "commit"}`."""
    return await _call_raspberry(business_id, "POST", "update", timeout=_UPDATE_TIMEOUT_SECONDS)


@router.get("/{business_id}/printer/devices", response_model=list[DeviceResponse])
async def get_printer_devices(business_id: str) -> list[DeviceResponse]:
    """Raspberries del negocio con su último heartbeat y el estado calculado ahora."""
    _get_configuration_or_404(business_id)
    return await run_in_threadpool(devices.list_devices, business_id)


@router.get("/{business_id}/printer/events", response_model=list[DeviceEventResponse])
async def get_printer_events(business_id: str, limit: int = Query(default=50, ge=1, le=200)) -> list[DeviceEventResponse]:
    """Historial de las Raspberries del negocio (cambios de conexión, impresora,
    Wi-Fi, versión y errores), del más reciente al más viejo."""
    _get_configuration_or_404(business_id)
    return await run_in_threadpool(devices.list_events, business_id, limit)


@router.post("/{business_id}/printer/test")
async def print_test_page(business_id: str) -> dict:
    """Imprime un ticket corto para comprobar la impresora de punta a punta
    (backend -> túnel -> Raspberry -> USB). 502 con el detalle si falla."""
    settings = dict(_get_configuration_or_404(business_id).printer_settings or {})
    now = datetime.now(DEFAULT_TZ).strftime("%Y-%m-%d %H:%M")
    # Sin tildes: la tabla de caracteres de la impresora térmica no siempre las trae.
    text = f"*** PRUEBA DE IMPRESION ***\n{now}\nSi lees esto, la impresora funciona.\n"
    body = {"text": text, "copies": 1}
    if settings.get("paper_width"):
        body["paper_width"] = settings["paper_width"]
    return await _call_raspberry(business_id, "POST", "print", timeout=15, json=body)


@router.post("/{business_id}/printer/wifi")
async def set_printer_wifi(business_id: str, payload: PrinterWifiRequest) -> dict:
    """Guarda en la Raspberry el Wi-Fi del negocio (ella no se cambia de red
    hasta que se apague el hotspot). La clave no se guarda aquí."""
    if not 1 <= len(payload.ssid.encode()) <= 32:
        raise HTTPException(status_code=422, detail="El nombre del Wi-Fi debe tener entre 1 y 32 bytes")
    if not 8 <= len(payload.password) <= 63:
        raise HTTPException(status_code=422, detail="La clave del Wi-Fi debe tener entre 8 y 63 caracteres")
    return await _call_raspberry(
        business_id, "POST", "wifi", timeout=15, json={"ssid": payload.ssid, "password": payload.password}
    )
