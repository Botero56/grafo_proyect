from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.enum_schemas import BussinessVertical
from app.services.plans import Plan


class BusinessCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    legal_name: str = Field(min_length=1, max_length=120)
    bussiness_vertical: BussinessVertical
    phone: str
    whatsapp_phone_number: str
    # Obligatorio: sin él, el webhook descarta los eventos del número
    # (firestore_webhook_service.get_business_by_phone).
    whatsapp_phone_number_id: str = Field(pattern=r"^\d{5,30}$")
    # None = WHATSAPP_PROVIDER global. "kapso" para números conectados por
    # un setup link de Kapso.
    whatsapp_provider: Optional[Literal["meta", "kapso"]] = None
    address: Optional[str] = None
    # None = premium (ver app/services/plans.py).
    plan: Optional[Plan] = None


class BusinessUpdateRequest(BaseModel):
    """Lo editable de un negocio ya creado. `status`, secretos,
    `webhook_tenant_key` y `bussiness_vertical` tienen su propio camino (o
    no se cambian): si llegan, se ignoran."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    legal_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    phone: Optional[str] = None
    whatsapp_phone_number: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = Field(default=None, pattern=r"^\d{5,30}$")
    whatsapp_provider: Optional[Literal["meta", "kapso"]] = None
    address: Optional[str] = Field(default=None, max_length=200)


class BusinessResponse(BaseModel):
    bussiness_id: str
    name: str
    legal_name: str
    bussiness_vertical: str
    phone: str
    whatsapp_phone_number: str
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_provider: Optional[str] = None
    address: Optional[str] = None
    status: str
    # Plan efectivo (sin plan guardado = premium) y lo que incluye, para que
    # el panel muestre u oculte cada sección.
    plan: str
    features: list[str]


class KapsoCredentialsRequest(BaseModel):
    """Alta o rotación de las credenciales de Kapso de un negocio. Los
    valores van directo a Secret Manager y nunca vuelven en una respuesta.
    Sin validaciones de pydantic sobre el contenido a propósito: un 422
    devolvería el valor recibido en el detalle del error."""

    # Key del proyecto de Kapso de este cliente (header X-API-Key).
    api_key: Optional[str] = None
    # `secret_key` con el que se registró el webhook del número en Kapso.
    webhook_secret: Optional[str] = None


class KapsoCredentialsResponse(BaseModel):
    api_key_configured: bool
    webhook_secret_configured: bool


class BotStatusRequest(BaseModel):
    status: Literal["active", "inactive"]
    # Quién lo cambia (correo o nombre). Lo declara el front: la API admin
    # tiene una sola key y el servidor no puede verificarlo (ver A7).
    actor: str = Field(min_length=1, max_length=120)
    reason: Optional[str] = Field(default=None, max_length=500)


class BotStatusResponse(BaseModel):
    status: str
    changed_at: Optional[datetime] = None
    changed_by: Optional[str] = None
    reason: Optional[str] = None


class PlanRequest(BaseModel):
    plan: Plan
    # Quién lo cambia. Igual que en BotStatusRequest: el panel pone el correo
    # que verificó Cloudflare (panel_entry.py).
    actor: str = Field(min_length=1, max_length=120)


class PlanResponse(BaseModel):
    plan: str
    features: list[str]
    changed_at: Optional[datetime] = None
    changed_by: Optional[str] = None


class WebhookUrlResponse(BaseModel):
    # None = el negocio aún no tiene clave (creado antes); POST .../rotate la genera.
    url: Optional[str] = None
    rotated_at: Optional[datetime] = None
