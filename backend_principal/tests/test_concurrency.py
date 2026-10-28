"""Carreras de la ingesta y del flush (B4 + A1), con un fake de Firestore que
devuelve copias (como Firestore real) y barreras que obligan a los procesos
a leer el mismo estado viejo antes de escribir.

shortcut: las garantías reales (create() con precondición, transacción del
compare-and-set) las da Firestore; aquí se prueba que el código las use bien.
test_concurrency_emulator.py las prueba contra el emulador si está corriendo.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.firestore_db.firebase_schemas import Conversations, Customers, Messages
from app.services.firestore_services import firestore_service
from app.services.firestore_services.firestore_webhook_service import WhatsAppWebhookService
from app.services.messaging import inbound_pipeline
from tests.conftest import DummyHandlers, customer_message_payload, seed_business
from tests.fakes import FakeFirestore


@pytest.fixture
def store():
    with FakeFirestore(snapshots=True) as fake:
        yield fake


def _barrier_on(attr: str, parties: int, only=None):
    """Envuelve firestore_service.<attr> para que las primeras `parties`
    llamadas esperen juntas: todas leyeron antes de que alguna escriba."""
    original = getattr(firestore_service, attr)
    barrier = threading.Barrier(parties, timeout=5)
    calls = {"n": 0}
    lock = threading.Lock()

    def wrapper(instance, *args):
        if only is None or type(instance) is only:
            with lock:
                calls["n"] += 1
                first = calls["n"] <= parties
            if first:
                barrier.wait()
        return original(instance, *args)

    return patch.object(firestore_service, attr, wrapper)


def _barrier_after_read(collection, parties: int):
    """Las primeras `parties` lecturas por filtro (`.filter(...).get()`)
    esperan juntas DESPUÉS de leer y antes de seguir: todos los procesos
    vieron "no existe" antes de que alguno escriba."""
    original = collection.filter
    barrier = threading.Barrier(parties, timeout=5)
    calls = {"n": 0}
    lock = threading.Lock()

    def filter_(**kwargs):
        chain = original(**kwargs)
        read = chain.get

        def get():
            result = read()
            with lock:
                calls["n"] += 1
                first = calls["n"] <= parties
            if first:
                barrier.wait()
            return result

        chain.get = get
        return chain

    return patch.object(collection, "filter", filter_)


def test_three_simultaneous_first_messages_create_one_customer_and_one_conversation(store):
    seed_business(store)
    service = WhatsAppWebhookService()
    payloads = [customer_message_payload(wamid=f"wamid.P{i}", text=f"mensaje {i}") for i in range(3)]

    with _barrier_after_read(Customers.collection, 3), ThreadPoolExecutor(3) as pool:
        results = list(pool.map(service.process_incoming_payload, payloads))

    assert len(store.stores[Customers]) == 1
    assert len(store.stores[Conversations]) == 1
    assert len({r["conversation"].conversation_id for r in results}) == 1
    assert len(store.stores[Messages]) == 3


def test_simultaneous_conversation_opening_converges_on_one_id(store):
    business = seed_business(store)
    service = WhatsAppWebhookService()
    customer = service._get_or_create_customer(business.bussiness_id, "573001112233", "Ana")

    with _barrier_on("create_if_absent", 3, only=Conversations), ThreadPoolExecutor(3) as pool:
        conversations = list(
            pool.map(lambda _: service._get_or_create_conversation(customer.business_id, customer.customer_id), range(3))
        )

    assert {c.conversation_id for c in conversations} == {f"{customer.customer_id}_c0"}
    assert len(store.stores[Conversations]) == 1


def test_a_closed_conversation_is_followed_by_a_new_id(store):
    business_id = seed_business(store).bussiness_id
    service = WhatsAppWebhookService()
    customer = service._get_or_create_customer(business_id, "573001112233", "Ana")
    first = service._get_or_create_conversation(business_id, customer.customer_id)
    store.stores[Conversations][first.conversation_id].status = "closed"

    second = service._get_or_create_conversation(business_id, customer.customer_id)

    assert second.conversation_id == f"{customer.customer_id}_c1"


def test_existing_customers_with_automatic_ids_are_still_found(store):
    business = seed_business(store)
    legacy = Customers()
    legacy.customer_id = "autoGeneradoViejo123"
    legacy.business_id = business.bussiness_id
    legacy.phone_number = "573009998877"
    store.seed(legacy)

    found = WhatsAppWebhookService()._get_or_create_customer(business.bussiness_id, "573009998877", "Ana")

    assert found.customer_id == "autoGeneradoViejo123"
    assert len(store.stores[Customers]) == 1


def test_duplicate_delivery_of_the_same_event_is_processed_once(store):
    seed_business(store)
    service = WhatsAppWebhookService()
    payload = customer_message_payload(wamid="wamid.DUP")

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(service.process_incoming_payload, [payload, payload]))

    # El segundo puede reprocesar (es indistinguible de un reintento de una
    # ingesta a medias), pero ambos apuntan al MISMO mensaje: el flush lo
    # contesta una vez.
    assert len(store.stores[Messages]) == 1
    assert len({r["message"].message_id for r in results if r and r.get("message")}) == 1


async def test_simultaneous_flushes_send_exactly_one_reply(store):
    seed_business(store)
    service = WhatsAppWebhookService()
    ingested = [
        service.process_incoming_payload(customer_message_payload(wamid=f"wamid.F{i}", text=f"parte {i}"))
        for i in range(3)
    ]
    ids = {
        "business_id": ingested[0]["business"].bussiness_id,
        "customer_id": ingested[0]["customer"].customer_id,
        "conversation_id": ingested[0]["conversation"].conversation_id,
    }
    whatsapp = MagicMock()
    whatsapp.mark_read = AsyncMock()

    with _barrier_on("compare_and_set", 3), patch.object(
        inbound_pipeline, "_seconds_until_quiet", return_value=0
    ), patch.object(inbound_pipeline, "get_vertical_handlers", return_value=DummyHandlers("cliente")), patch.object(
        inbound_pipeline.WhatsAppClient, "for_business", return_value=whatsapp
    ), patch.object(
        inbound_pipeline.menu_image, "send_menu_image", new=AsyncMock(return_value=False)
    ), patch.object(inbound_pipeline, "_generate_and_send_reply", new=AsyncMock()) as reply:
        answered = await asyncio.gather(*(inbound_pipeline.flush_conversation(**ids) for _ in range(3)))

    assert sorted(answered) == [False, False, True]
    reply.assert_awaited_once()
    stored = store.stores[Conversations][ids["conversation_id"]]
    newest = max(m.created_at for m in store.stores[Messages].values())
    assert stored.ultimo_mensaje_respondido_at == newest


def test_a_lost_compare_and_set_rereads_and_claims_only_what_is_new(store):
    """El perdedor del compare-and-set relee la marca: si entretanto llegó
    un mensaje nuevo, se lleva solo ese."""
    business = seed_business(store)
    service = WhatsAppWebhookService()
    first = service.process_incoming_payload(customer_message_payload(wamid="wamid.L1", text="uno"))
    conversation_id = first["conversation"].conversation_id

    stale = Conversations.collection.get(conversation_id)  # lectura vieja (sin marca)
    assert [m.content for m in inbound_pipeline._claim_pending_messages(Conversations.collection.get(conversation_id))] == ["uno"]
    late = Messages()
    late.business_id = business.bussiness_id
    late.conversation_id = conversation_id
    late.content = "dos"
    late.type = "text"
    late.created_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    store.seed(late)

    assert [m.content for m in inbound_pipeline._claim_pending_messages(stale)] == ["dos"]
