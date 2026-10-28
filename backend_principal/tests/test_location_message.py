"""Una ubicación compartida llega al agente como texto (A5)."""

from app.services.firestore_services.firestore_webhook_service import WhatsAppWebhookService


def test_location_becomes_text_with_a_maps_link():
    content, media_id, _ = WhatsAppWebhookService._extract_message_content(
        WhatsAppWebhookService.__new__(WhatsAppWebhookService),
        "location",
        {"location": {"latitude": 6.25, "longitude": -75.56, "name": "Torre 3", "address": "Cra 1 #2-3"}},
    )

    assert content == "[Ubicación compartida] Torre 3, Cra 1 #2-3 https://maps.google.com/?q=6.25,-75.56"
    assert media_id is None


def test_location_without_name_keeps_the_coordinates():
    content, _, _ = WhatsAppWebhookService._extract_message_content(
        WhatsAppWebhookService.__new__(WhatsAppWebhookService), "location", {"location": {"latitude": 1, "longitude": 2}}
    )

    assert content == "[Ubicación compartida] https://maps.google.com/?q=1,2"
