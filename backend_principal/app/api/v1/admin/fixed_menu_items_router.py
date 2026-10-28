from fastapi import APIRouter, Depends, HTTPException, status

from app.api.v1.deps import require_api_key
from app.firestore_db.firebase_schemas import Businesses, FixedMenuItems
from app.schemas.api.daily_menu_schemas import (
    FixedMenuItemCreateRequest,
    FixedMenuItemResponse,
    FixedMenuItemUpdateRequest,
)
from app.services.firestore_services.firestore_service import FireStoreCRUDService
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery

router = APIRouter(dependencies=[Depends(require_api_key)])


def _get_fixed_item_or_404(business_id: str, fixed_item_id: str) -> FixedMenuItems:
    item = TenantScopedQuery.get_by_id_for_business(FixedMenuItems, business_id, fixed_item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Producto fijo no encontrado para ese negocio",
        )
    return item


@router.post("", response_model=FixedMenuItemResponse, status_code=status.HTTP_201_CREATED)
def create_fixed_menu_item(payload: FixedMenuItemCreateRequest) -> FixedMenuItemResponse:
    if Businesses.collection.get(payload.business_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Negocio no encontrado")
    item = FireStoreCRUDService().create(
        FixedMenuItems,
        business_id=payload.business_id,
        nombre=payload.nombre,
        descripcion=payload.descripcion,
        tipo_precio=payload.tipo_precio,
        precio=payload.precio,
        categoria=payload.categoria,
        is_active=payload.is_active,
    )
    return FixedMenuItemResponse(**item.to_dict())


@router.get("/{business_id}", response_model=list[FixedMenuItemResponse])
def list_fixed_menu_items(business_id: str) -> list[FixedMenuItemResponse]:
    items = TenantScopedQuery.list_for_business(FixedMenuItems, business_id)
    return [FixedMenuItemResponse(**item.to_dict()) for item in items]


@router.patch("/{fixed_item_id}", response_model=FixedMenuItemResponse)
def update_fixed_menu_item(
    fixed_item_id: str, business_id: str, payload: FixedMenuItemUpdateRequest
) -> FixedMenuItemResponse:
    item = _get_fixed_item_or_404(business_id, fixed_item_id)

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(item, field, value)

    FireStoreCRUDService().update(item)
    return FixedMenuItemResponse(**item.to_dict())


@router.delete("/{fixed_item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_fixed_menu_item(fixed_item_id: str, business_id: str) -> None:
    item = _get_fixed_item_or_404(business_id, fixed_item_id)
    FireStoreCRUDService().delete(item)
