"""El worker tiene dos rutas y la diferencia entre ellas es el diseño:
`/tasks/process-event` ingesta y agenda, `/tasks/flush-conversation`
contesta. Lo que se protege acá es que la ingesta NO se quede esperando la
ventana de agrupado (eso causaba 504 en Cloud Run) y que igual nadie se
quede sin respuesta."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.services import worker_entry

client = TestClient(worker_entry.app)

_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "WABA1",
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "metadata": {"phone_number_id": "1234567890"},
                        "messages": [{"id": "wamid.A", "from": "573001234567", "type": "text"}],
                    },
                }
            ],
        }
    ],
}

_PENDING = {"business_id": "b1", "customer_id": "c1", "conversation_id": "conv-1"}


def test_ingest_hands_the_payload_to_the_pipeline():
    with patch.object(
        worker_entry.inbound_pipeline, "process_incoming_message", new=AsyncMock(return_value=None)
    ) as process, patch.object(worker_entry.cloud_tasks_client, "enqueue_flush") as enqueue:
        response = client.post(
            "/tasks/process-event", json=_PAYLOAD, headers={"X-CloudTasks-QueueName": "q"}
        )

    assert response.status_code == 200
    assert process.await_args.args[0] == _PAYLOAD
    enqueue.assert_not_called()  # None = nada pendiente


def test_ingest_unwraps_the_envelope_from_the_tenant_webhook():
    with patch.object(
        worker_entry.inbound_pipeline, "process_incoming_message", new=AsyncMock(return_value=None)
    ) as process:
        response = client.post(
            "/tasks/process-event",
            json={"business_id": "b1", "event": _PAYLOAD},
            headers={"X-CloudTasks-QueueName": "q"},
        )

    assert response.status_code == 200
    assert process.await_args.args == (_PAYLOAD, "b1")


def test_ingest_schedules_the_flush_when_a_reply_is_pending():
    with patch.object(
        worker_entry.inbound_pipeline,
        "process_incoming_message",
        new=AsyncMock(return_value=_PENDING),
    ), patch.object(worker_entry.cloud_tasks_client, "enqueue_flush") as enqueue:
        response = client.post(
            "/tasks/process-event", json=_PAYLOAD, headers={"X-CloudTasks-QueueName": "q"}
        )

    assert response.status_code == 200
    payload, delay = enqueue.call_args.args
    assert payload == _PENDING
    assert delay > 0  # la espera la hace la cola, no esta request


def test_an_unexpected_ingest_error_answers_200():
    """Un error fuera de la ingesta puede ocurrir después de haberle escrito
    al cliente (comprobante, menú): reintentarlo duplicaría ese mensaje."""
    with patch.object(
        worker_entry.inbound_pipeline,
        "process_incoming_message",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        response = client.post(
            "/tasks/process-event", json=_PAYLOAD, headers={"X-CloudTasks-QueueName": "q"}
        )

    assert response.status_code == 200


@pytest.mark.parametrize("retry_count, status", [("0", 500), ("4", 500), ("5", 200)])
def test_a_retryable_failure_asks_cloud_tasks_to_retry_up_to_a_cap(retry_count, status):
    with patch.object(
        worker_entry.inbound_pipeline,
        "process_incoming_message",
        new=AsyncMock(side_effect=worker_entry.inbound_pipeline.RetryableTaskError()),
    ):
        response = client.post(
            "/tasks/process-event",
            json=_PAYLOAD,
            headers={"X-CloudTasks-QueueName": "q", "X-CloudTasks-TaskRetryCount": retry_count},
        )

    assert response.status_code == status


def test_if_the_flush_cannot_be_scheduled_the_task_answers_itself():
    """Regresión: si Cloud Tasks fallaba al agendar el flush, el mensaje
    quedaba guardado y nadie lo contestaba."""
    with patch.object(
        worker_entry.inbound_pipeline, "process_incoming_message", new=AsyncMock(return_value=_PENDING)
    ), patch.object(
        worker_entry.cloud_tasks_client, "enqueue_flush", side_effect=RuntimeError("cola caída")
    ), patch.object(worker_entry.asyncio, "sleep", new=AsyncMock()), patch.object(
        worker_entry.inbound_pipeline, "flush_conversation", new=AsyncMock(return_value=True)
    ) as flush:
        response = client.post(
            "/tasks/process-event", json=_PAYLOAD, headers={"X-CloudTasks-QueueName": "q"}
        )

    assert response.status_code == 200
    flush.assert_awaited_once_with(**_PENDING)

def test_flush_route_answers_the_conversation():
    with patch.object(
        worker_entry.inbound_pipeline, "flush_conversation", new=AsyncMock(return_value=True)
    ) as flush:
        response = client.post(
            "/tasks/flush-conversation", json=_PENDING, headers={"X-CloudTasks-QueueName": "q"}
        )

    assert response.status_code == 200
    assert flush.await_args.kwargs == _PENDING


def test_flush_route_answers_200_even_if_the_turn_fails():
    with patch.object(
        worker_entry.inbound_pipeline,
        "flush_conversation",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        response = client.post(
            "/tasks/flush-conversation", json=_PENDING, headers={"X-CloudTasks-QueueName": "q"}
        )

    assert response.status_code == 200


def test_missing_cloud_tasks_header_is_only_a_warning():
    """El control de acceso real es IAM/OIDC en Cloud Run, no esta cabecera:
    el emulador local no la manda y el worker igual debe procesar."""
    with patch.object(
        worker_entry.inbound_pipeline, "process_incoming_message", new=AsyncMock(return_value=None)
    ):
        response = client.post("/tasks/process-event", json=_PAYLOAD)

    assert response.status_code == 200
