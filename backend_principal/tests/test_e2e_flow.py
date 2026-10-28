"""Flujo completo de la arquitectura separada, de punta a punta:

    Meta -> webhook_entry (/webhook) -> Cloud Tasks -> worker (/tasks/process-event)
         -> Cloud Tasks (schedule_time) -> worker (/tasks/flush-conversation)
         -> agente -> WhatsApp

Los tests de cada pieza (test_webhook_entry, test_worker_entry,
test_flush_conversation) mockean a la vecina. Acá corren todas juntas con
el código real: firma, split, router de eventos, ingesta en Firestore,
resolución de rol, claim con marca de agua, envío y persistencia de la
respuesta. Solo se reemplazan los bordes externos: la cola (FakeQueue),
Firestore (FakeFirestore), Gemini (_agent_runner) y la Graph API de
WhatsApp (WhatsAppClient.send_text_and_get_id).
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.ai.core.agent_runner import AgentReply
from app.config import whatsapp_settings
from app.firestore_db.firebase_schemas import Conversations, Messages
from app.schemas.enum_schemas import MessageDirection, MessageOrigin
from app.services import webhook_entry, worker_entry
from app.services.tasks import cloud_tasks_client
from tests.conftest import (
    CUSTOMER_PHONE,
    customer_message_payload,
    echo_payload,
    seed_business,
    seed_configuration,
    sign,
)

from app.services.messaging import inbound_pipeline


@pytest.fixture(autouse=True)
def _customers_already_accepted():
    """Estos tests son del camino de respuesta; la autorización de datos que
    se pide en el primer mensaje se prueba en test_privacy.py."""
    with patch("app.services.privacy.has_consent", return_value=True):
        yield

webhook_client = TestClient(webhook_entry.app)
worker_client = TestClient(worker_entry.app)


class FakeQueue:
    """Cola en memoria con la misma forma que cloud_tasks_client.

    `enqueue_event` deduplica por wamid igual que el nombre determinista de
    la tarea real (AlreadyExists); `enqueue_flush` no deduplica, igual que
    la real. `run()` entrega las tareas al worker en orden de llegada, que
    es lo que haría la cola cuando vence cada schedule_time.
    """

    def __init__(self) -> None:
        self.tasks: list[tuple[str, dict]] = []
        self._event_names: set[str] = set()

    def enqueue_event(self, payload: dict, event_id: str, business_id=None) -> bool:
        if event_id in self._event_names:
            return False
        self._event_names.add(event_id)
        self.tasks.append(("/tasks/process-event", {"business_id": business_id, "event": payload}))
        return True

    def enqueue_flush(self, payload: dict, delay_seconds: float) -> None:
        assert delay_seconds > 0
        self.tasks.append(("/tasks/flush-conversation", payload))

    def run(self) -> list[str]:
        delivered = []
        while self.tasks:
            path, payload = self.tasks.pop(0)
            response = worker_client.post(
                path, json=payload, headers={"X-CloudTasks-QueueName": "test"}
            )
            assert response.status_code == 200
            delivered.append(path)
        return delivered


@pytest.fixture
def flow(fake_store):
    business = seed_business(fake_store)
    seed_configuration(fake_store, business)
    queue = FakeQueue()

    agent = AsyncMock(return_value=AgentReply(text="Hoy hay bandeja paisa y ajiaco."))
    send = AsyncMock(return_value="wamid.OUT1")
    notify = AsyncMock(return_value=True)
    handoff = AsyncMock(return_value=True)

    # La cola entrega cada flush cuando vence su schedule_time, o sea con el
    # cliente ya callado: sin esto la ventana deslizante lo pospondría siempre.
    with patch.object(cloud_tasks_client, "enqueue_event", queue.enqueue_event), patch.object(
        cloud_tasks_client, "enqueue_flush", queue.enqueue_flush
    ), patch.object(inbound_pipeline, "_seconds_until_quiet", return_value=0), patch.object(
        inbound_pipeline._agent_runner, "get_reply", new=agent
    ), patch.object(
        inbound_pipeline._agent_runner, "append_human_message", new=AsyncMock()
    ), patch(
        "app.services.whatsapp_client.whatsapp_client.WhatsAppClient.send_text_and_get_id",
        new=send,
    ), patch.object(inbound_pipeline._handoff_service, "notify_human", new=notify), patch.object(
        inbound_pipeline._handoff_service, "notify_handoff", new=handoff
    ):
        yield {
            "store": fake_store,
            "business": business,
            "queue": queue,
            "agent": agent,
            "send": send,
            "handoff": handoff,
            "notify": notify,
        }


def _post_from_meta(body: dict, *, signed: bool = True):
    raw = json.dumps(body)
    headers = {"Content-Type": "application/json"}
    if signed:
        headers["X-Hub-Signature-256"] = sign(body, whatsapp_settings.APP_SECRET)
    return webhook_client.post("/webhook", content=raw, headers=headers)


def _messages(store, **filters) -> list:
    return [
        m
        for m in store.stores[Messages].values()
        if all(getattr(m, k, None) == v for k, v in filters.items())
    ]


def test_customer_message_is_answered_once_by_the_agent(flow):
    response = _post_from_meta(customer_message_payload(text="¿Qué hay de almuerzo?"))
    assert response.status_code == 200

    delivered = flow["queue"].run()

    assert delivered == ["/tasks/process-event", "/tasks/flush-conversation"]
    flow["agent"].assert_awaited_once()
    assert flow["agent"].await_args.kwargs["message_text"] == "¿Qué hay de almuerzo?"

    flow["send"].assert_awaited_once()
    to_phone, text = flow["send"].await_args.args
    assert to_phone == CUSTOMER_PHONE
    assert text == "Hoy hay bandeja paisa y ajiaco."

    # Queda el mensaje del cliente y la respuesta de la IA, ambos en la misma conversación.
    replies = _messages(flow["store"], direction=MessageDirection.SALIENTE.value)
    assert len(replies) == 1
    assert replies[0].origin == MessageOrigin.IA.value
    assert replies[0].whatsapp_message_id == "wamid.OUT1"
    incoming = _messages(flow["store"], whatsapp_message_id="wamid.CUSTOMER1")
    assert len(incoming) == 1
    assert incoming[0].conversation_id == replies[0].conversation_id


def test_burst_of_messages_gets_a_single_combined_reply(flow):
    for i, text in enumerate(["hola", "ya llegué", "¿qué tienen hoy?"]):
        _post_from_meta(customer_message_payload(wamid=f"wamid.B{i}", text=text))

    delivered = flow["queue"].run()

    # Tres ingestas y tres flush agendados; solo el primer flush contesta.
    assert delivered.count("/tasks/flush-conversation") == 3
    flow["agent"].assert_awaited_once()
    assert flow["agent"].await_args.kwargs["message_text"] == "hola\nya llegué\n¿qué tienen hoy?"
    flow["send"].assert_awaited_once()


def test_meta_redelivery_is_not_processed_twice(flow):
    body = customer_message_payload(wamid="wamid.DUP")
    _post_from_meta(body)
    _post_from_meta(body)
    flow["queue"].run()

    # Aun si la cola no la frenara, WebhookEvents la frena en el worker.
    flow["queue"]._event_names.clear()
    _post_from_meta(body)
    flow["queue"].run()

    assert len(_messages(flow["store"], whatsapp_message_id="wamid.DUP")) == 1
    flow["agent"].assert_awaited_once()
    flow["send"].assert_awaited_once()


def test_invalid_signature_stops_everything_at_the_door(flow):
    response = _post_from_meta(customer_message_payload(), signed=False)

    assert response.status_code == 401
    assert flow["queue"].tasks == []
    assert flow["store"].stores[Messages] == {}


def test_human_echo_pauses_the_bot_and_the_next_message_goes_to_the_human(flow):
    _post_from_meta(customer_message_payload(wamid="wamid.C1", text="hola"))
    flow["queue"].run()
    flow["agent"].reset_mock()
    flow["send"].reset_mock()

    _post_from_meta(echo_payload(wamid="wamid.E1", text="Hola, te atiendo yo"))
    flow["queue"].run()

    conversation = next(iter(flow["store"].stores[Conversations].values()))
    assert conversation.bot_activo is False

    _post_from_meta(customer_message_payload(wamid="wamid.C2", text="¿sigues ahí?"))
    delivered = flow["queue"].run()

    assert delivered == ["/tasks/process-event"]  # sin flush: nada para la IA
    flow["agent"].assert_not_awaited()
    flow["send"].assert_not_awaited()
    flow["notify"].assert_awaited_once()
    # El mensaje igual queda guardado para que el humano lo vea.
    assert len(_messages(flow["store"], whatsapp_message_id="wamid.C2")) == 1


def test_ai_handoff_sends_the_transfer_message_and_keeps_the_bot_active(flow):
    flow["agent"].return_value = AgentReply(
        text="texto libre del modelo",
        requires_human=True,
        handoff_reason="solicitud_cliente",
        handoff_summary="Reclamo por un pedido incompleto.",
    )

    _post_from_meta(customer_message_payload(text="quiero hablar con una persona"))
    flow["queue"].run()

    flow["send"].assert_awaited_once()
    assert flow["send"].await_args.args[1] != "texto libre del modelo"
    # La encargada recibe la nota corta con el resumen que escribió la IA.
    flow["handoff"].assert_awaited_once()
    assert flow["handoff"].await_args.kwargs["resumen"] == "Reclamo por un pedido incompleto."
    conversation = next(iter(flow["store"].stores[Conversations].values()))
    assert conversation.bot_activo is True
