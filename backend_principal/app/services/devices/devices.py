"""Raspberries de los negocios: guardar el último heartbeat y derivar su estado.

El heartbeat entra por el webhook público (verifica el token del negocio y
encola) y lo guarda el worker en `/tasks/device-heartbeat`.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.firestore_db.firebase_schemas import DeviceEvents, Devices
from app.schemas.api.device_schemas import DeviceEventResponse, DeviceResponse, HeartbeatPayload
from app.schemas.enum_schemas import DeviceEventType, DeviceStatus
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery

# La Raspberry manda cada 60 s: se tolera perder 3 seguidos antes de
# llamarla desconectada (un heartbeat perdido no es una caída).
STALE_AFTER = timedelta(seconds=210)


def _doc_id(business_id: str, device_id: str) -> str:
    return f"{business_id}--{device_id}"


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def record_heartbeat(business_id: str, payload: HeartbeatPayload, now: Optional[datetime] = None) -> Devices:
    now = now or datetime.now(timezone.utc)
    doc_id = _doc_id(business_id, payload.device_id)
    previous = Devices.collection.get(doc_id)
    device = Devices()
    device.device_doc_id = doc_id
    device.business_id = business_id
    device.last_seen_at = now
    for field, value in payload.model_dump().items():
        setattr(device, field, value)
    # Eventos antes que el estado: si el save del estado falla, Cloud Tasks
    # reintenta y como mucho repite un evento (al revés se perdería).
    for event_type, at, until, detail in detect_events(previous, device, now):
        event = DeviceEvents()
        event.business_id = business_id
        event.device_id = payload.device_id
        event.event_type = event_type.value
        event.at, event.until, event.detail = at, until, detail
        event.save()
    # save() completo: es el estado actual, cada heartbeat lo reemplaza.
    device.save()
    return device


def _minutes(seconds: float) -> str:
    if seconds < 3600:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"


def detect_events(previous: Optional[Devices], current: Devices, now: datetime) -> list[tuple]:
    """Qué cambió entre el heartbeat anterior y este: (tipo, desde, hasta, detalle)."""
    if previous is None:
        return [(DeviceEventType.REGISTRADA, now, None, f"Primera conexión (versión {current.version})")]
    events = []
    seen = _aware(previous.last_seen_at)
    gap = (now - seen).total_seconds() if seen else 0
    uptime, prev_uptime = current.uptime_seconds, previous.uptime_seconds
    # uptime es del sistema: solo baja si la Raspberry se apagó o reinició
    # (reiniciar el servicio en /update no lo resetea).
    rebooted = uptime is not None and (uptime < gap or (prev_uptime is not None and uptime < prev_uptime))
    if seen and gap > STALE_AFTER.total_seconds():
        cause = "se apagó o se fue la luz (la Raspberry se reinició)" if rebooted else "se fue el internet"
        events.append((DeviceEventType.SIN_CONEXION, seen, now, f"Sin conexión {_minutes(gap)}: {cause}"))
    elif rebooted:
        events.append((DeviceEventType.REINICIADA, now, None, "La Raspberry se reinició"))
    if previous.printer_usb and not current.printer_usb:
        events.append((DeviceEventType.IMPRESORA_DESCONECTADA, now, None, "Impresora USB desconectada"))
    elif previous.printer_usb is False and current.printer_usb:
        events.append((DeviceEventType.IMPRESORA_CONECTADA, now, None, "Impresora USB conectada"))
    if previous.wifi_ssid != current.wifi_ssid:
        events.append((DeviceEventType.WIFI_CAMBIADO, now, None,
                       f"Wi-Fi: {previous.wifi_ssid or 'sin Wi-Fi'} → {current.wifi_ssid or 'sin Wi-Fi'}"))
    if previous.version and previous.version != current.version:
        events.append((DeviceEventType.VERSION_ACTUALIZADA, now, None, f"Versión {previous.version} → {current.version}"))
    error_at, prev_error_at = _aware(current.last_error_at), _aware(previous.last_error_at)
    if error_at and (prev_error_at is None or error_at > prev_error_at):
        events.append((DeviceEventType.ERROR_IMPRESION, error_at, None, current.last_error or "Error al imprimir"))
    return events


def list_events(business_id: str, limit: int = 50) -> list[DeviceEventResponse]:
    # ponytail: trae todos los eventos del negocio y ordena aquí (son pocos: solo cambios);
    # filtrar por fecha en Firestore si un negocio llega a miles.
    events = TenantScopedQuery.list_for_business(DeviceEvents, business_id)
    events.sort(key=lambda e: _aware(e.at) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return [
        DeviceEventResponse(device_id=e.device_id, event_type=e.event_type, at=_aware(e.at),
                            until=_aware(e.until), detail=e.detail)
        for e in events[:limit]
    ]


def device_status(device: Devices, now: Optional[datetime] = None) -> DeviceStatus:
    now = now or datetime.now(timezone.utc)
    seen = _aware(device.last_seen_at)
    if seen is None:
        return DeviceStatus.SIN_INFORMACION
    if now - seen > STALE_AFTER:
        return DeviceStatus.DESCONECTADO
    if not device.printer_usb:
        return DeviceStatus.DEGRADADO
    error_at, ok_at = _aware(device.last_error_at), _aware(device.last_print_ok_at)
    if error_at and (ok_at is None or error_at > ok_at):
        return DeviceStatus.DEGRADADO
    return DeviceStatus.OPERATIVO


def to_response(device: Devices, now: Optional[datetime] = None) -> DeviceResponse:
    now = now or datetime.now(timezone.utc)
    seen = _aware(device.last_seen_at)
    return DeviceResponse(
        device_id=device.device_id,
        business_id=device.business_id,
        status=device_status(device, now).value,
        last_seen_at=seen,
        seconds_since_seen=int((now - seen).total_seconds()) if seen else None,
        version=device.version,
        commit=device.commit,
        printer_usb=device.printer_usb,
        wifi_ssid=device.wifi_ssid,
        uptime_seconds=device.uptime_seconds,
        last_print_ok_at=_aware(device.last_print_ok_at),
        last_error=device.last_error,
        last_error_at=_aware(device.last_error_at),
    )


def list_devices(business_id: str) -> list[DeviceResponse]:
    now = datetime.now(timezone.utc)
    devices = TenantScopedQuery.list_for_business(Devices, business_id)
    return [to_response(device, now) for device in sorted(devices, key=lambda d: d.device_id or "")]
