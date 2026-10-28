"""La forma exacta de la tarea es el contrato con Cloud Tasks y con el
worker: el nombre determinista deduplica los reintentos de Meta, el OIDC es
lo que deja pasar la tarea al Cloud Run privado, y schedule_time es la
ventana de agrupado. test_webhook_entry/test_worker_entry mockean estas
funciones enteras, así que esto es lo único que las ejecuta."""

import hashlib
import json
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2

from app.services.tasks import cloud_tasks_client

QUEUE = "projects/p/locations/us-central1/queues/whatsapp-messages"


@pytest.fixture
def client(monkeypatch):
    fake = MagicMock()
    fake.queue_path.return_value = QUEUE
    monkeypatch.setattr(cloud_tasks_client, "get_client", lambda: fake)
    settings = cloud_tasks_client.cloud_tasks_settings
    monkeypatch.setattr(settings, "CLOUD_TASKS_EMULATOR_HOST", "")
    monkeypatch.setattr(settings, "WORKER_URL", "https://worker.run.app/")
    monkeypatch.setattr(settings, "WORKER_SERVICE_ACCOUNT_EMAIL", "tasks@p.iam.gserviceaccount.com")
    return fake


def _sent_request(fake) -> dict:
    return fake.create_task.call_args.kwargs["request"]


def test_event_task_is_named_after_the_wamid_and_signed_with_oidc(client):
    payload = {"entry": [{"id": "W1"}]}

    assert cloud_tasks_client.enqueue_event(payload, "wamid.A/B", business_id="b1") is True

    assert _sent_request(client) == {
        "parent": QUEUE,
        "task": {
            "name": f"{QUEUE}/tasks/{hashlib.sha256(b'wamid.A/B').hexdigest()}",
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": "https://worker.run.app/tasks/process-event",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"business_id": "b1", "event": payload}).encode("utf-8"),
                "oidc_token": {"service_account_email": "tasks@p.iam.gserviceaccount.com", "audience": "https://worker.run.app"},
            },
        },
    }


def test_redelivered_event_is_reported_as_already_queued(client):
    client.create_task.side_effect = AlreadyExists("ya existe")

    assert cloud_tasks_client.enqueue_event({"entry": []}, "wamid.A") is False


def test_without_service_account_there_is_no_oidc(client, monkeypatch):
    monkeypatch.setattr(cloud_tasks_client.cloud_tasks_settings, "WORKER_SERVICE_ACCOUNT_EMAIL", "")

    cloud_tasks_client.enqueue_event({"entry": []}, "wamid.A")

    assert "oidc_token" not in _sent_request(client)["task"]["http_request"]


def test_flush_task_is_scheduled_unnamed_and_targets_the_flush_route(client):
    payload = {"business_id": "b1", "customer_id": "c1", "conversation_id": "conv-1"}

    cloud_tasks_client.enqueue_flush(payload, delay_seconds=5)

    task = _sent_request(client)["task"]
    assert "name" not in task  # cada mensaje agenda su propio flush
    assert task["schedule_time"].seconds > 0
    assert task["http_request"] == {
        "http_method": tasks_v2.HttpMethod.POST,
        "url": "https://worker.run.app/tasks/flush-conversation",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload).encode("utf-8"),
        "oidc_token": {"service_account_email": "tasks@p.iam.gserviceaccount.com", "audience": "https://worker.run.app"},
    }
