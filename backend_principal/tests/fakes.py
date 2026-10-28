"""Fake ligero de Firestore/fireo para tests.

No es un emulador completo: solo implementa lo que el código bajo prueba
usa -- `Model.collection.get(id)`, `Model.collection.filter(**kwargs).get()`
/`.fetch()`, y `.save()`/`.update()` en instancias -- para no pegarle a
Firestore real (ni requerir credenciales) en cada test.
"""

import copy
import threading
import uuid
from unittest import mock

from app.firestore_db.firebase_schemas import (
    Businesses,
    CatalogMappings,
    Configurations,
    DeviceEvents,
    Devices,
    Conversations,
    Customers,
    DailyMenus,
    DailyOrderCounters,
    DailyStock,
    FixedMenuItems,
    Incidents,
    Messages,
    Orders,
    OrderStatusHistory,
    PrintJobs,
    WebhookEvents,
)

_ID_ATTR = {
    Businesses: "bussiness_id",
    Customers: "customer_id",
    Conversations: "conversation_id",
    Messages: "message_id",
    WebhookEvents: "webhook_event_id",
    Configurations: "configuration_id",
    # DailyMenus vive en la colección Firestore "menu_items" (unificada con
    # el viejo catálogo plano, ver firebase_schemas.py) -- el nombre de la
    # clase Python no cambió, solo la colección física.
    DailyMenus: "daily_menu_id",
    FixedMenuItems: "fixed_item_id",
    Orders: "order_id",
    PrintJobs: "print_job_id",
    OrderStatusHistory: "order_status_id",
    DailyOrderCounters: "counter_id",
    DailyStock: "stock_id",
    Incidents: "incident_id",
    CatalogMappings: "catalog_mapping_id",
    Devices: "device_doc_id",
    DeviceEvents: "device_event_id",
}


_OPS = {
    "==": lambda a, b: a == b,
    ">=": lambda a, b: a is not None and a >= b,
    ">": lambda a, b: a is not None and a > b,
    "<=": lambda a, b: a is not None and a <= b,
    "<": lambda a, b: a is not None and a < b,
}


class _FilterChain:
    def __init__(self, store: dict, kwargs: dict, read=lambda doc: doc, condition=None) -> None:
        self._store = store
        self._kwargs = kwargs
        self._read = read
        # filter("campo", ">=", valor), como fireo
        self._condition = condition

    def _matches(self, doc) -> bool:
        if self._condition:
            field, op, value = self._condition
            if not _OPS[op](getattr(doc, field, None), value):
                return False
        return all(getattr(doc, k, None) == v for k, v in self._kwargs.items())

    def get(self):
        for doc in list(self._store.values()):
            if self._matches(doc):
                return self._read(doc)
        return None

    def fetch(self) -> list:
        return [self._read(doc) for doc in list(self._store.values()) if self._matches(doc)]


class _FakeCollection:
    def __init__(self, store: dict, read=lambda doc: doc) -> None:
        self._store = store
        self._read = read

    def get(self, doc_id, transaction=None):
        doc = self._store.get(doc_id)
        return self._read(doc) if doc is not None else None

    def filter(self, *condition, **kwargs) -> _FilterChain:
        return _FilterChain(self._store, kwargs, self._read, condition or None)

    def fetch(self, limit=None) -> list:
        return [self._read(doc) for doc in list(self._store.values())][:limit]

    def delete(self, key: str) -> None:
        self._store.pop(key.split("/")[-1], None)


class FakeFirestore:
    """Context manager: parchea save/update/collection de los modelos de
    firebase_schemas.py para que vivan en memoria durante el test.

    Uso:
        with FakeFirestore() as fake:
            fake.stores[Businesses][biz.bussiness_id] = biz
            ...
    """

    def __init__(self, snapshots: bool = False) -> None:
        """`snapshots=True`: cada lectura devuelve una copia y cada escritura
        guarda una copia, como Firestore real. Hace falta para reproducir
        carreras (dos procesos con lecturas viejas); por defecto se comparte
        la instancia, que es lo que asumen los tests de siempre."""
        self.stores: dict[type, dict[str, object]] = {model: {} for model in _ID_ATTR}
        self._patches: list = []
        self._snapshots = snapshots
        self._lock = threading.Lock()
        self._txn_lock = threading.RLock()

    def _copy(self, doc):
        return copy.deepcopy(doc) if self._snapshots else doc

    def _make_saver(self, id_attr: str, store: dict):
        def _save(instance, transaction=None, **_):
            if not getattr(instance, id_attr, None):
                setattr(instance, id_attr, uuid.uuid4().hex)
            store[getattr(instance, id_attr)] = self._copy(instance)
            return instance

        return _save

    def _create_if_absent(self, instance) -> bool:
        """Como DocumentReference.create(): atómico, falla si el id existe."""
        store = self.stores[type(instance)]
        doc_id = getattr(instance, _ID_ATTR[type(instance)])
        with self._lock:
            if doc_id in store:
                return False
            store[doc_id] = self._copy(instance)
            return True

    def _run_in_transaction(self, fn):
        """Serializa las transacciones (Firestore reintenta las que chocan; el
        resultado es el mismo que correrlas una tras otra)."""
        with self._txn_lock:
            return fn(None)

    def _compare_and_set(self, instance, field, expected, new) -> bool:
        stored = self.stores[type(instance)].get(getattr(instance, _ID_ATTR[type(instance)]))
        with self._lock:
            if stored is None or getattr(stored, field, None) != expected:
                return False
            setattr(stored, field, new)
            return True

    def __enter__(self) -> "FakeFirestore":
        for model, id_attr in _ID_ATTR.items():
            store = self.stores[model]
            saver = self._make_saver(id_attr, store)

            p_save = mock.patch.object(model, "save", saver, create=True)
            p_update = mock.patch.object(model, "update", saver, create=True)
            p_collection = mock.patch.object(
                model, "collection", _FakeCollection(store, self._copy), create=True
            )
            self._patches += [p_save, p_update, p_collection]

        from app.services.firestore_services import firestore_service

        self._patches += [
            mock.patch.object(firestore_service, "create_if_absent", self._create_if_absent),
            mock.patch.object(firestore_service, "compare_and_set", self._compare_and_set),
            mock.patch.object(firestore_service, "run_in_transaction", self._run_in_transaction),
        ]

        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc) -> None:
        for p in reversed(self._patches):
            p.stop()

    def seed(self, instance) -> object:
        """Guarda una instancia ya armada (con o sin ID) en el store correspondiente."""
        model = type(instance)
        store = self.stores[model]
        id_attr = _ID_ATTR[model]
        if not getattr(instance, id_attr, None):
            setattr(instance, id_attr, uuid.uuid4().hex)
        store[getattr(instance, id_attr)] = instance  # el test conserva la misma instancia
        return instance
