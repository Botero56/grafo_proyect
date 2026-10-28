"""
Acceso a Firestore con scoping por tenant (business_id).

FireStoreCRUDService es genérico y no todos los modelos tienen business_id,
así que el filtrado por tenant no vive ahí. Esta clase centraliza el patrón
que ya usaba firestore_webhook_service.py (Model.collection.filter(...)),
para que cada endpoint nuevo no tenga que repetirlo y no pueda olvidar el
filtro por negocio.
"""

from typing import Optional, Type, TypeVar

from fireo.models import Model

from app.services.firestore_services.firestore_service import FireStoreCRUDService

T = TypeVar("T", bound=Model)


class TenantScopedQuery:

    @staticmethod
    def list_for_business(model: Type[T], business_id: str, **filters) -> list[T]:
        # list(): .fetch() de fireo devuelve un QueryIterator, no una
        # lista (no implementa __bool__/__len__ y se agota al recorrerlo
        # una vez) -- se materializa acá para que el tipo de retorno
        # declarado (list[T]) sea real y cualquier caller pueda hacerle
        # len()/if not .../recorrerlo más de una vez sin sorpresas.
        return list(model.collection.filter(business_id=business_id, **filters).fetch())

    @staticmethod
    def get_one_for_business(model: Type[T], business_id: str, **filters) -> Optional[T]:
        return model.collection.filter(business_id=business_id, **filters).get()

    @staticmethod
    def get_by_id_for_business(
        model: Type[T], business_id: str, document_id: str
    ) -> Optional[T]:
        obj = FireStoreCRUDService().get(model, document_id)
        if obj is None or getattr(obj, "business_id", None) != business_id:
            return None
        return obj
