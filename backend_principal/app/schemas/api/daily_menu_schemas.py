from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

# PriceType (app.schemas.enum_schemas).
PriceTypeLiteral = Literal["fijo", "no_legible"]
# Pesos enteros: es lo que se le cobra al cliente.
Price = Field(default=None, ge=0, le=10_000_000)


class FixedMenuItemCreateRequest(BaseModel):
    business_id: str
    nombre: str = Field(min_length=1, max_length=120)
    descripcion: Optional[str] = Field(default=None, max_length=500)
    tipo_precio: PriceTypeLiteral = "fijo"
    precio: Optional[int] = Price
    categoria: str = Field(default="Fijos", min_length=1, max_length=60)
    is_active: bool = True

    @model_validator(mode="after")
    def _fijo_con_precio(self):
        if self.tipo_precio == "fijo" and self.precio is None:
            raise ValueError("Un producto de precio fijo necesita precio")
        return self


class FixedMenuItemUpdateRequest(BaseModel):
    nombre: Optional[str] = Field(default=None, min_length=1, max_length=120)
    descripcion: Optional[str] = Field(default=None, max_length=500)
    tipo_precio: Optional[PriceTypeLiteral] = None
    precio: Optional[int] = Price
    categoria: Optional[str] = Field(default=None, min_length=1, max_length=60)
    is_active: Optional[bool] = None


class FixedMenuItemResponse(BaseModel):
    fixed_item_id: str
    business_id: str
    nombre: str
    descripcion: Optional[str] = None
    tipo_precio: str
    precio: Optional[float] = None
    moneda: str = "COP"
    categoria: str
    is_active: bool
    # retailer_id del catálogo de WhatsApp: con valor, el producto lo
    # mantiene la sincronización (catalog_sync) y se pisa al sincronizar.
    catalog_retailer_id: Optional[str] = None


class DailyMenuSweepResponse(BaseModel):
    checked: int
    reminded: list
    discarded: Optional[list] = None
