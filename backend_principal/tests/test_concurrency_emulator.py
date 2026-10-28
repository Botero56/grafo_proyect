"""create_if_absent y compare_and_set contra el emulador de Firestore: la
semántica real de create() y de la transacción, que el fake solo imita.

Se omite sin FIRESTORE_EMULATOR_HOST (nunca corre contra producción):

    gcloud emulators firestore start --host-port=localhost:8086   # Java 11+
    FIRESTORE_EMULATOR_HOST=localhost:8086 APP_SECRET=test pytest tests/test_concurrency_emulator.py
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("FIRESTORE_EMULATOR_HOST"), reason="sin emulador de Firestore")


def test_create_if_absent_lets_exactly_one_writer_win():
    from app.firestore_db.firebase_schemas import Customers
    from app.services.firestore_services import firestore_service

    doc_id = f"emu_{uuid.uuid4().hex}"

    def attempt(i):
        customer = Customers()
        customer.customer_id = doc_id
        customer.name = f"intento {i}"
        return firestore_service.create_if_absent(customer)

    with ThreadPoolExecutor(5) as pool:
        assert sorted(pool.map(attempt, range(5))) == [False] * 4 + [True]


def test_compare_and_set_rejects_a_stale_expected_value():
    from app.firestore_db.firebase_schemas import Conversations
    from app.services.firestore_services import firestore_service

    conversation = Conversations()
    conversation.conversation_id = f"emu_{uuid.uuid4().hex}"
    conversation.status = "active"
    assert firestore_service.create_if_absent(conversation)
    first = datetime.now(timezone.utc)

    assert firestore_service.compare_and_set(conversation, "ultimo_mensaje_respondido_at", None, first)
    assert not firestore_service.compare_and_set(conversation, "ultimo_mensaje_respondido_at", None, first + timedelta(seconds=1))
    stored = Conversations.collection.get(conversation.conversation_id)
    assert stored.ultimo_mensaje_respondido_at == first
    assert firestore_service.compare_and_set(
        conversation, "ultimo_mensaje_respondido_at", stored.ultimo_mensaje_respondido_at, first + timedelta(seconds=2)
    )


def test_daily_codes_are_unique_under_real_transactions():
    from app.firestore_db.firebase_schemas import Orders
    from app.services.delivery import tracking

    business_id = f"emu_{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    orders = []
    for _ in range(10):
        order = Orders(business_id=business_id, status="en_cocina", created_at=now)
        order.save()
        orders.append(order)

    with ThreadPoolExecutor(5) as pool:
        codes = list(pool.map(lambda o: tracking.assign_daily_code(o, now), orders))
    assert sorted(codes) == list(range(1, 11))


def test_two_couriers_settling_the_same_code_never_both_win():
    from app.firestore_db.firebase_schemas import Orders
    from app.services.delivery import tracking

    business_id = f"emu_{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    order = Orders(business_id=business_id, status="en_cocina", created_at=now)
    order.save()
    tracking.assign_daily_code(order, now)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda p: tracking.settle(business_id, p, [1], "liquidacion", now), ["573001110000", "573002220000"]))
    assert sorted(len(r["ok"]) for r in results) == [0, 1]
    assert sorted(len(r["conflicts"]) for r in results) == [0, 1]
