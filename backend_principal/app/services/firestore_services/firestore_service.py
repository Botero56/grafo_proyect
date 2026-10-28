import logging

import fireo
from google.cloud import firestore
from datetime import datetime,timezone
from fireo.models import Model
from typing import Optional,Type, TypeVar
from app.config import firestore_settings, FirestoreSettings

T = TypeVar("T", bound=Model)

logger = logging.getLogger(__name__)

_connected = False
_client: Optional[firestore.Client] = None


def ensure_firestore_connection(credentials: FirestoreSettings = firestore_settings) -> None:
    """Conecta fireo a Firestore UNA vez por proceso.

    Antes cada servicio (FireStoreCRUDService, WhatsAppWebhookService,
    FirestoreReportRepository) creaba su propio firestore.Client al
    instanciarse, y se instancian en cada request: ~2 s de resolución de
    credenciales cada vez (medido 2026-09-30). fireo guarda una sola
    conexión global, así que reconectar no aportaba nada.

    Solo se marca como conectado si salió bien: si falla, la próxima
    llamada lo reintenta, igual que antes. `credentials` se usa solo en esa
    primera conexión -- todo el proyecto usa la misma base.
    """
    global _connected, _client
    if _connected:
        return
    try:
        client = firestore.Client(project=credentials.PROJECT_ID, database=credentials.DATABASE)
        fireo.connection(client=client)
        _client = client
        _connected = True
    except Exception as e:
        logger.error("Error en conexión Firestore: %s", e)


def create_if_absent(instance: Model) -> bool:
    """Crea el documento con el id que ya trae `instance` SOLO si no existe.
    La precondición la aplica Firestore, así que es atómico: si dos procesos
    crean el mismo id a la vez, uno gana y el otro recibe False. Escribe los
    mismos campos que `save()`."""
    from fireo.queries.create_query import CreateQuery
    from google.api_core.exceptions import AlreadyExists

    ensure_firestore_connection()
    query = CreateQuery(type(instance), instance)
    try:
        query._doc_ref().create(query._parse_field())
    except AlreadyExists:
        return False
    instance._reset_field_changed()
    return True


def compare_and_set(instance: Model, field: str, expected, new) -> bool:
    """Escribe `field = new` en el documento de `instance` solo si en
    Firestore sigue valiendo `expected` (transacción). False si otro proceso
    lo cambió antes: quien llama relee y decide. Solo toca ese campo."""
    ensure_firestore_connection()
    ref = _client.collection(instance._meta.collection_name).document(instance._id)

    @firestore.transactional
    def _run(transaction) -> bool:
        snapshot = ref.get(transaction=transaction)
        current = (snapshot.to_dict() or {}).get(field) if snapshot.exists else None
        if current != expected:
            return False
        transaction.update(ref, {field: new})
        return True

    return _run(_client.transaction())


def run_in_transaction(fn):
    """Corre `fn(transaction)` en una transacción de Firestore: si otro proceso
    toca lo leído, Firestore la reintenta entera. Dentro de `fn` se lee con
    `Model.collection.get(id, transaction=t)` y se escribe con
    `instance.save(transaction=t)`, todas las lecturas antes de la primera
    escritura."""
    ensure_firestore_connection()
    return fireo.transactional(fn)(fireo.transaction())


class FireStoreCRUDService:

    def __init__(self, credentials: FirestoreSettings = firestore_settings) -> None:

        ensure_firestore_connection(credentials)


    def create(
        self,
        model: Type[T],
        parent: Optional[Model] = None,
        **data
    ) -> T:

        if parent is not None:
            data["parent"] = parent.key

        obj = model(**data)
        obj.save()

        return obj

    def get(
        self,
        model: Type[T],
        document_id: str
    ) -> Optional[T]:

        return model.collection.get(document_id)

    def update(
        self,
        obj: T
    ) -> T:

        obj.save()

        return obj

    def delete(
        self,
        obj: T
    ) -> None:

        # fireo borra por la clave desde el manager; la instancia no tiene .delete().
        type(obj).collection.delete(obj.key)
