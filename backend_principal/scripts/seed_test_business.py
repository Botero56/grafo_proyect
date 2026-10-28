"""
Crea (o reutiliza si ya existe) el Business de prueba que corresponde al
número de test que entrega el Cloud API sandbox de Meta, para poder empezar
a probar el flujo de WhatsApp -> webhook -> IA de punta a punta.

whatsapp_phone_number debe ser igual a metadata.display_phone_number que
manda WhatsApp en el payload del webhook (ver
WhatsAppWebhookService.process_incoming_payload, paso 4), y
whatsapp_phone_number_id se toma de PHONE_NUMBER_ID en el .env, que ya
corresponde a este mismo número de test.

Uso: uv run python scripts/seed_test_business.py
"""

from datetime import datetime

from app.config import whatsapp_settings
from app.firestore_db.firebase_schemas import Businesses, Configurations
from app.schemas.enum_schemas import BusinessStatus, BussinessVertical
from app.services.firestore_services.firestore_service import FireStoreCRUDService

TEST_WHATSAPP_NUMBER = "15556684257"
TEST_BUSINESS_NAME = "Restaurante de Prueba"


def seed_test_business() -> Businesses:
    crud = FireStoreCRUDService()

    existing = Businesses.collection.filter(whatsapp_phone_number=TEST_WHATSAPP_NUMBER).get()
    if existing:
        print(f"Ya existe: {existing.name} (bussiness_id={existing.bussiness_id})")
        return existing

    now = datetime.now()
    business = crud.create(
        Businesses,
        name=TEST_BUSINESS_NAME,
        legal_name=TEST_BUSINESS_NAME,
        bussiness_vertical=BussinessVertical.RESTAURANT.value,
        phone=TEST_WHATSAPP_NUMBER,
        whatsapp_phone_number=TEST_WHATSAPP_NUMBER,
        whatsapp_phone_number_id=whatsapp_settings.PHONE_NUMBER_ID,
        status=BusinessStatus.ACTIVE.value,
        created_at=now,
        updated_at=now,
    )

    crud.create(
        Configurations,
        configuration_id=business.bussiness_id,
        business_id=business.bussiness_id,
        admin_phone_numbers=[],
    )

    print(f"Creado: {business.name} (bussiness_id={business.bussiness_id})")
    print(f"whatsapp_phone_number={TEST_WHATSAPP_NUMBER}")
    print(f"whatsapp_phone_number_id={whatsapp_settings.PHONE_NUMBER_ID}")
    return business


if __name__ == "__main__":
    seed_test_business()
