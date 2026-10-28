from datetime import date
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.delivery.zones import normalize


class DeliveryZone(BaseModel):
    nombre: str = Field(min_length=1, max_length=80)
    # Pesos enteros: es lo que se le cobra al cliente.
    tarifa: int = Field(ge=0, le=1_000_000)
    alias: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("nombre")
    @classmethod
    def _nombre_no_vacio(cls, value: str) -> str:
        if not normalize(value):
            raise ValueError("El nombre de la zona es obligatorio")
        return value.strip()

    @field_validator("alias")
    @classmethod
    def _alias_largos(cls, value: list[str]) -> list[str]:
        # Un alias corto ("la") aparece en casi cualquier dirección.
        short = [a for a in value if len(normalize(a)) < 4]
        if short:
            raise ValueError(f"Cada alias necesita 4 caracteres o más: {short}")
        return [a.strip() for a in value]


def check_unique_zones(zones: list[DeliveryZone]) -> list[DeliveryZone]:
    """Nombres y alias no se repiten entre zonas: si una dirección coincide
    con dos zonas, find_zone no cobra ninguna (ver delivery/zones.py)."""
    seen: dict[str, str] = {}
    for zone in zones:
        for name in dict.fromkeys(normalize(n) for n in [zone.nombre, *zone.alias]):
            if name in seen:
                raise ValueError(f"'{name}' está en dos zonas: {seen[name]} y {zone.nombre}")
            seen[name] = zone.nombre
    return zones


class CuentaTransferencia(BaseModel):
    banco: str = Field(min_length=1, max_length=60)
    tipo: Literal["ahorros", "corriente", "billetera"]
    numero: str = Field(pattern=r"^\d{6,20}$")
    titular: str = Field(min_length=1, max_length=120)
    llave: Optional[str] = Field(default=None, max_length=120)


class FaqEntry(BaseModel):
    pregunta: str = Field(min_length=1, max_length=200)
    respuesta: str = Field(min_length=1, max_length=1000)


_HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DayKey = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class TimeRange(BaseModel):
    # close <= open = cierra al día siguiente (ej. 18:00-02:00).
    open: str = Field(pattern=_HHMM)
    close: str = Field(pattern=_HHMM)

    @model_validator(mode="after")
    def _not_empty(self):
        if self.open == self.close:
            raise ValueError("open y close no pueden ser iguales (24 h: 00:00-23:59)")
        return self


class HoursException(BaseModel):
    date: date
    closed: bool = False
    ranges: list[TimeRange] = Field(default_factory=list)

    @model_validator(mode="after")
    def _closed_or_ranges(self):
        if self.closed == bool(self.ranges):
            raise ValueError("Una excepción es closed=true o trae ranges, no ambas ni ninguna")
        return self


class OpeningHours(BaseModel):
    timezone: str = "America/Bogota"
    weekly: dict[DayKey, list[TimeRange]] = Field(default_factory=dict)
    exceptions: list[HoursException] = Field(default_factory=list)
    # True = no se confirman pedidos fuera de horario; False = solo se informa.
    enforce: bool = True

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Zona horaria desconocida (ej. America/Bogota)")
        return value


class TemplateRef(BaseModel):
    # Nombre tal cual quedó aprobado en Meta/Kapso.
    name: str = Field(pattern=r"^[a-z0-9_]{1,512}$")
    language: str = Field(default="es", pattern=r"^[a-z]{2,3}(_[A-Z]{2})?$")


AdminTemplateKind = Literal["comanda_respaldo", "cotizacion_domicilio", "atencion_cliente", "recordatorio_menu", "cierre_diario"]


class ConfigurationUpdateRequest(BaseModel):
    # Lo que no está acá se ignora si llega: la impresora va por PUT
    # .../printer, admins y domiciliarios por sus PUT, y los campos
    # obsoletos (delivery_confirmation_*, has_thermal_printer...) ya no
    # se exponen.
    restaurant_name: Optional[str] = Field(default=None, max_length=80)
    manager_phone: Optional[str] = None
    manager_name: Optional[str] = Field(default=None, max_length=60)
    orders_to_admin_chat: Optional[bool] = None
    # Domicilios (ver app/services/delivery/tracking.py).
    tiempo_entrega_estimado_min: Optional[int] = Field(default=None, ge=1, le=600)
    hora_cierre_domicilios: Optional[str] = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    incidencia_espera_min: Optional[int] = Field(default=None, ge=1, le=60)
    incidencia_reintentos: Optional[int] = Field(default=None, ge=0, le=3)
    # tono, faq y el mensaje de traspaso entran tal cual al prompt o al
    # chat del cliente: con tope de tamaño.
    handoff_transfer_message: Optional[str] = Field(default=None, max_length=500)
    delivery_zones: Optional[list[DeliveryZone]] = Field(default=None, max_length=100)
    whatsapp_catalog_id: Optional[str] = Field(default=None, pattern=r"^\d{1,30}$")
    enviar_menu_al_iniciar: Optional[bool] = None
    daily_menu_expected_ready_time: Optional[str] = Field(default=None, pattern=_HHMM)
    tono: Optional[str] = Field(default=None, max_length=500)
    metodos_pago: Optional[list[Literal["efectivo", "transferencia"]]] = None
    cuentas_transferencia: Optional[list[CuentaTransferencia]] = Field(default=None, max_length=5)
    recordatorio_menu_hora: Optional[str] = Field(default=None, pattern=_HHMM)
    # Va en un parámetro de plantilla: una sola línea, sin punto final
    # (la plantilla ya lo pone). Vacío = texto por defecto.
    recordatorio_menu_texto: Optional[str] = Field(default=None, max_length=300)
    pedido_minimo: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    faq: Optional[list[FaqEntry]] = Field(default=None, max_length=50)
    opening_hours: Optional[OpeningHours] = None
    admin_templates: Optional[dict[AdminTemplateKind, TemplateRef]] = None

    @field_validator("delivery_zones")
    @classmethod
    def _zonas_unicas(cls, value):
        return value if value is None else check_unique_zones(value)

    @field_validator("recordatorio_menu_texto")
    @classmethod
    def _una_linea(cls, value):
        # WhatsApp rechaza saltos de línea en un parámetro de plantilla.
        text = " ".join((value or "").split()).rstrip(".")
        return text or None


class PrinterWifiRequest(BaseModel):
    """Wi-Fi del negocio para la Raspberry. Sin validaciones de pydantic a
    propósito: un 422 devolvería la clave en el detalle del error; el router
    valida con mensajes fijos."""

    ssid: str
    password: str


class PrinterSettingsRequest(BaseModel):
    """Alta o cambio de la impresora del negocio. `token` es de solo
    escritura: va a Secret Manager y nunca vuelve en una respuesta. Sin
    validaciones de pydantic sobre el token a propósito: un 422 devolvería
    el valor recibido en el detalle del error."""

    enabled: bool
    endpoint_url: Optional[str] = None
    # Omitido = conserva el token actual (rotar = mandar uno nuevo).
    token: Optional[str] = None
    paper_width: Optional[Literal["58mm", "80mm"]] = None
    copies: int = Field(default=1, ge=1, le=5)
    timeout_seconds: int = Field(default=15, ge=3, le=30)


class PrinterSettingsResponse(BaseModel):
    enabled: bool = False
    endpoint_url: Optional[str] = None
    token_configured: bool = False
    paper_width: Optional[str] = None
    copies: int = 1
    timeout_seconds: int = 15


class AdminPhoneNumbersUpdateRequest(BaseModel):
    phone_numbers: list[str]


class CourierEntry(BaseModel):
    phone: str
    name: str = Field(min_length=1, max_length=60)


class CouriersUpdateRequest(BaseModel):
    couriers: list[CourierEntry] = Field(max_length=100)


class ConfigurationResponse(BaseModel):
    configuration_id: str
    business_id: str
    restaurant_name: Optional[str] = None
    manager_phone: Optional[str] = None
    manager_name: Optional[str] = None
    admin_phone_numbers: list[str] = []
    couriers: list[CourierEntry] = []
    orders_to_admin_chat: bool = False
    handoff_transfer_message: Optional[str] = None
    delivery_zones: list[DeliveryZone] = []
    whatsapp_catalog_id: Optional[str] = None
    enviar_menu_al_iniciar: Optional[bool] = None
    daily_menu_expected_ready_time: Optional[str] = None
    tono: Optional[str] = None
    metodos_pago: list[str] = []
    cuentas_transferencia: list[CuentaTransferencia] = []
    recordatorio_menu_hora: Optional[str] = None
    recordatorio_menu_texto: Optional[str] = None
    pedido_minimo: Optional[float] = None
    faq: list[FaqEntry] = []
    opening_hours: Optional[OpeningHours] = None
    admin_templates: Optional[dict[str, TemplateRef]] = None
    tiempo_entrega_estimado_min: Optional[int] = None
    hora_cierre_domicilios: Optional[str] = None
    incidencia_espera_min: Optional[int] = None
    incidencia_reintentos: Optional[int] = None
