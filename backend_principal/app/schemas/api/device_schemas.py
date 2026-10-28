from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

# Ids que viajan en el nombre del documento y de la tarea.
DEVICE_ID_PATTERN = r"^[A-Za-z0-9_.-]{1,64}$"
BUSINESS_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class HeartbeatPayload(BaseModel):
    """Lo que manda la Raspberry (backend_rasperry app/telemetry.py). Sin
    secretos: el token viaja en la cabecera y nunca se guarda."""

    device_id: str = Field(pattern=DEVICE_ID_PATTERN)
    measured_at: datetime
    version: str = Field(max_length=40)
    commit: Optional[str] = Field(default=None, max_length=40)
    agent: str = Field(default="ok", max_length=20)
    printer_usb: bool
    wifi_ssid: Optional[str] = Field(default=None, max_length=64)
    uptime_seconds: Optional[int] = Field(default=None, ge=0)
    last_print_ok_at: Optional[datetime] = None
    last_error: Optional[str] = Field(default=None, max_length=300)
    last_error_at: Optional[datetime] = None


class DeviceEventResponse(BaseModel):
    device_id: str
    event_type: str
    at: datetime
    until: Optional[datetime]
    detail: Optional[str]


class DeviceResponse(BaseModel):
    device_id: str
    business_id: str
    status: str
    last_seen_at: Optional[datetime]
    seconds_since_seen: Optional[int]
    version: Optional[str]
    commit: Optional[str]
    printer_usb: Optional[bool]
    wifi_ssid: Optional[str]
    uptime_seconds: Optional[int]
    last_print_ok_at: Optional[datetime]
    last_error: Optional[str]
    last_error_at: Optional[datetime]
