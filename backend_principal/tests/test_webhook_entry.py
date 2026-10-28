"""El ingress solo verifica firma, separa y encola. Lo importante acá es lo
que NO hace: no toca Firestore, no llama al agente, y no puede perder un
evento sin que la respuesta lo refleje."""

import hashlib
import hmac
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import whatsapp_settings
from app.services import webhook_entry

client = TestClient(webhook_entry.app)


def _signed(body: dict) -> tuple[str, dict]:
    raw = json.dumps(body)
    digest = hmac.new(
        whatsapp_settings.APP_SECRET.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return raw, {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


def _message_body(*wamids: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {
                                "display_phone_number": "573000000000",
                                "phone_number_id": "1234567890",
                            },
                            "contacts": [{"wa_id": "573001234567", "profile": {"name": "Ana"}}],
                            "messages": [
                                {
                                    "id": w,
                                    "from": "573001234567",
                                    "type": "text",
                                    "text": {"body": "hola"},
                                }
                                for w in wamids
                            ],
                        },
                    }
                ],
            }
        ],
    }


def test_get_webhook_verifies_with_the_right_token():
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": whatsapp_settings.VERIFY_TOKEN,
            "hub.challenge": "desafio123",
        },
    )

    assert response.status_code == 200
    assert response.text == "desafio123"


def test_get_webhook_rejects_a_wrong_token():
    response = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "x"},
    )

    assert response.status_code == 403


def test_unsigned_payload_is_rejected_and_nothing_is_enqueued():
    with patch.object(webhook_entry.cloud_tasks_client, "enqueue_event") as enqueue:
        response = client.post("/webhook", json=_message_body("wamid.A"))

    assert response.status_code == 401
    enqueue.assert_not_called()


def test_invalid_signature_is_rejected_and_nothing_is_enqueued():
    with patch.object(webhook_entry.cloud_tasks_client, "enqueue_event") as enqueue:
        response = client.post(
            "/webhook",
            json=_message_body("wamid.A"),
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
        )

    assert response.status_code == 401
    enqueue.assert_not_called()


def test_valid_signature_over_a_different_body_is_rejected():
    # Firma calculada sobre un body DISTINTO al que realmente se manda.
    _, headers = _signed({"object": "otro"})
    with patch.object(webhook_entry.cloud_tasks_client, "enqueue_event") as enqueue:
        response = client.post(
            "/webhook", content=json.dumps(_message_body("wamid.A")), headers=headers
        )

    assert response.status_code == 401
    enqueue.assert_not_called()


def test_each_message_is_enqueued_once_with_its_wamid():
    raw, headers = _signed(_message_body("wamid.A", "wamid.B"))

    with patch.object(
        webhook_entry.cloud_tasks_client, "enqueue_event", return_value=True
    ) as enqueue:
        response = client.post("/webhook", content=raw, headers=headers)

    assert response.status_code == 200
    assert [call.args[1] for call in enqueue.call_args_list] == ["wamid.A", "wamid.B"]
    # Cada tarea lleva SU mensaje, no el sobre entero.
    for call in enqueue.call_args_list:
        payload = call.args[0]
        assert len(payload["entry"][0]["changes"][0]["value"]["messages"]) == 1


def test_status_payload_is_not_enqueued():
    body = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "1234567890"},
                            "statuses": [{"id": "wamid.S1", "status": "delivered"}],
                        },
                    }
                ],
            }
        ],
    }
    raw, headers = _signed(body)

    with patch.object(webhook_entry.cloud_tasks_client, "enqueue_event") as enqueue:
        response = client.post("/webhook", content=raw, headers=headers)

    assert response.status_code == 200
    enqueue.assert_not_called()


def test_enqueue_failure_answers_502_so_meta_retries():
    raw, headers = _signed(_message_body("wamid.A"))

    with patch.object(
        webhook_entry.cloud_tasks_client, "enqueue_event", side_effect=RuntimeError("cola caída")
    ):
        response = client.post("/webhook", content=raw, headers=headers)

    assert response.status_code == 502


def test_body_that_is_not_a_json_object_is_rejected():
    raw = json.dumps(["no", "soy", "un", "objeto"])
    digest = hmac.new(
        whatsapp_settings.APP_SECRET.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    response = client.post(
        "/webhook",
        content=raw,
        headers={"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"},
    )

    assert response.status_code == 400


def test_a_failed_delivery_status_is_logged_as_an_error(caplog):
    """Regresión: los statuses se descartaban sin mirar, y "failed" es la
    única forma en que Meta avisa que una respuesta aceptada NO llegó."""
    body = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "statuses": [
                                {"id": "wamid.OK", "status": "delivered"},
                                {
                                    "id": "wamid.F1",
                                    "status": "failed",
                                    "errors": [{"code": 131047, "title": "Re-engagement message"}],
                                },
                            ],
                        },
                    }
                ],
            }
        ],
    }
    raw, headers = _signed(body)

    with patch.object(webhook_entry.cloud_tasks_client, "enqueue_event") as enqueue:
        response = client.post("/webhook", content=raw, headers=headers)

    assert response.status_code == 200
    enqueue.assert_not_called()
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert errors == ["WhatsApp no entregó el mensaje wamid.F1: code=131047 Re-engagement message"]
