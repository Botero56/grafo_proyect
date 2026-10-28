import hashlib
import hmac
import json
import time

import pytest

from app.firestore_db.firebase_schemas import (
    Businesses,
    Configurations,
    Conversations,
    Customers,
    DailyMenus,
)
from app.schemas.enum_schemas import DailyMenuStatus
from app.services.daily_menu.caption_date_resolver import resolve_fecha_vigencia
from app.services.daily_menu.vision_extractor import (
    Acompanamiento,
    ExtractedMenu,
    MenuDelDia,
    OtrasOpciones,
)
from tests.fakes import FakeFirestore

BUSINESS_PHONE = "15550101234"
CUSTOMER_PHONE = "573001234567"
PHONE_NUMBER_ID = "123456789"
WABA_ID = "880622108811"


@pytest.fixture(autouse=True)
def _tasks_as_in_local_emulator(monkeypatch):
    """Como en docker-compose: con el emulador, el worker no exige el token
    OIDC de Cloud Tasks. test_task_auth lo prueba apagándolo."""
    from app.config import cloud_tasks_settings

    monkeypatch.setattr(cloud_tasks_settings, "CLOUD_TASKS_EMULATOR_HOST", "localhost:8123")


@pytest.fixture
def fake_store():
    from app.services import secrets

    with FakeFirestore() as fake:
        yield fake
    secrets.clear_cache()


def seed_business(fake_store, **overrides) -> Businesses:
    business = Businesses()
    business.name = "Edy Gourmet"
    business.legal_name = "Edy Gourmet SAS"
    business.bussiness_vertical = "restaurant"
    business.whatsapp_phone_number = BUSINESS_PHONE
    business.whatsapp_phone_number_id = PHONE_NUMBER_ID
    business.status = "active"
    for key, value in overrides.items():
        setattr(business, key, value)
    return fake_store.seed(business)


def seed_configuration(fake_store, business: Businesses, **overrides) -> Configurations:
    configuration = Configurations()
    configuration.business_id = business.bussiness_id
    configuration.manager_phone = "573009999999"
    configuration.admin_phone_numbers = []
    for key, value in overrides.items():
        setattr(configuration, key, value)
    return fake_store.seed(configuration)


PRINTER_URL = "https://printer.test/print"
PRINTER_TOKEN = "token-impresora-de-prueba-01"


def seed_printer(fake_store, business: Businesses, url: str = PRINTER_URL, token: str = PRINTER_TOKEN) -> Configurations:
    """Impresora habilitada para el negocio, con su token en la caché de
    secretos (como si se hubiera leído de Secret Manager)."""
    from app.services import secrets

    ref = f"projects/p/secrets/printer-token--{business.bussiness_id}"
    secrets._cache[(ref, False)] = ([token], float("inf"))
    configuration = next(
        (c for c in fake_store.stores.get(Configurations, {}).values() if c.business_id == business.bussiness_id),
        None,
    ) or seed_configuration(fake_store, business)
    configuration.printer_settings = {"enabled": True, "endpoint_url": url, "auth_secret_ref": ref}
    return configuration


def seed_customer(fake_store, business: Businesses, phone: str = CUSTOMER_PHONE, **overrides) -> Customers:
    customer = Customers()
    customer.business_id = business.bussiness_id
    customer.phone_number = phone
    # Ya aceptó la política de datos: los tests que la prueban pasan None.
    from app.services.privacy import POLICY_VERSION
    customer.data_consent_version = POLICY_VERSION
    for key, value in overrides.items():
        setattr(customer, key, value)
    return fake_store.seed(customer)


def seed_conversation(fake_store, business: Businesses, customer_id: str, **overrides) -> Conversations:
    conversation = Conversations()
    conversation.business_id = business.bussiness_id
    conversation.customer_id = customer_id
    conversation.status = "active"
    for key, value in overrides.items():
        setattr(conversation, key, value)
    return fake_store.seed(conversation)


class FakeToolContext:
    """Lo único que las tools leen de ADK es `state`, que el runner llena
    con business_id/customer_id (ver AgentRunnerService.get_reply)."""

    def __init__(self, business_id: str, customer_id: str | None = None, conversation_id: str | None = None) -> None:
        self.state = {"business_id": business_id, "customer_id": customer_id, "conversation_id": conversation_id}


class DummyHandlers:
    """Reemplaza get_vertical_handlers: rol fijo y un agente que nunca se
    ejecuta (get_reply se mockea aparte)."""

    def __init__(self, role: str, customer_id: str | None = None) -> None:
        self._role = role
        self._customer_id = customer_id

    def resolve_role(self, **kwargs):
        return {"role": self._role, "customer_id": self._customer_id}

    def get_agent(self, role):
        return object()


def make_menu(
    *,
    nombre: str = "Pollo al curry con champiñones",
    precio: float | None = 18500,
    tipo_precio: str = "fijo",
    acompanamientos: list[Acompanamiento] | None = None,
    proteinas: list[str] | None = None,
    otras_precio: float | None = 20500,
    otras_tipo: str = "fijo",
    envios: bool | None = True,
    telefono: str | None = "3235099092",
    fecha_detectada: str | None = None,
) -> ExtractedMenu:
    """El menú real del negocio (ver el ejemplo de vision_extractor.py);
    cada test cambia solo lo que está probando."""
    return ExtractedMenu(
        menu_del_dia=MenuDelDia(nombre=nombre, precio=precio, tipo_precio=tipo_precio),
        acompanamientos=acompanamientos
        if acompanamientos is not None
        else [
            Acompanamiento(nombre="Sopa", opciones=["Ajiaco", "Frijoles"]),
            Acompanamiento(nombre="Arroz", opciones=[]),
        ],
        otras_opciones=OtrasOpciones(
            proteinas=proteinas or ["Cerdo", "Chicharrón", "Pollo", "Res"],
            precio=otras_precio,
            tipo_precio=otras_tipo,
        ),
        envios=envios,
        telefono_contacto=telefono,
        fecha_detectada=fecha_detectada,
        advertencias=[],
        confianza=0.9,
    )


def seed_active_menu(fake_store, business: Businesses, fecha_vigencia=None, **overrides) -> DailyMenus:
    """Menú del día ACTIVO (por defecto, el de hoy) con make_menu();
    `overrides` reemplaza claves del extracted_menu."""
    daily = DailyMenus()
    daily.business_id = business.bussiness_id
    daily.fecha_vigencia = fecha_vigencia or resolve_fecha_vigencia(None)
    daily.status = DailyMenuStatus.ACTIVO.value
    daily.schema_version = 3
    daily.extracted_menu = make_menu().model_dump()
    for key, value in overrides.items():
        daily.extracted_menu[key] = value
    return fake_store.seed(daily)


async def ingest_and_reply(result: dict) -> None:
    """Lo que hace el worker con sus dos tareas, sin la cola en medio:
    `_process_customer_message` (ingesta) y, si quedó algo por contestar,
    `flush_conversation`. Los mensajes del contexto necesitan
    `conversation_id` y `created_at` para que el flush los reclame."""
    from app.services.messaging import inbound_pipeline

    pending = await inbound_pipeline._process_customer_message(result)
    if pending:
        await inbound_pipeline.flush_conversation(**pending)


def customer_message_payload(
    wamid: str = "wamid.CUSTOMER1",
    from_phone: str = CUSTOMER_PHONE,
    to_phone: str = BUSINESS_PHONE,
    text: str = "Hola, ¿qué tienen de almuerzo hoy?",
) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": WABA_ID,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": to_phone,
                                "phone_number_id": PHONE_NUMBER_ID,
                            },
                            "contacts": [
                                {"profile": {"name": "Juan Perez"}, "wa_id": from_phone}
                            ],
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": wamid,
                                    "timestamp": str(int(time.time())),
                                    "text": {"body": text},
                                    "type": "text",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def status_update_payload(
    wamid: str = "wamid.CUSTOMER1",
    status: str = "delivered",
    to_phone: str = BUSINESS_PHONE,
) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": WABA_ID,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": to_phone,
                                "phone_number_id": PHONE_NUMBER_ID,
                            },
                            "statuses": [
                                {
                                    "id": wamid,
                                    "status": status,
                                    "timestamp": str(int(time.time())),
                                    "recipient_id": CUSTOMER_PHONE,
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def echo_payload(
    wamid: str = "wamid.ECHO1",
    from_phone: str = BUSINESS_PHONE,
    to_phone: str = CUSTOMER_PHONE,
    text: str = "Ya te ayudo en un momento",
) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": WABA_ID,
                "changes": [
                    {
                        "field": "smb_message_echoes",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": from_phone,
                                "phone_number_id": PHONE_NUMBER_ID,
                            },
                            "message_echoes": [
                                {
                                    "from": from_phone,
                                    "to": to_phone,
                                    "id": wamid,
                                    "timestamp": str(int(time.time())),
                                    "text": {"body": text},
                                    "type": "text",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def sign(body: dict, app_secret: str) -> str:
    raw = json.dumps(body).encode("utf-8")
    digest = hmac.new(app_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return f"sha256={digest}"
