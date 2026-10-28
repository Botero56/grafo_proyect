import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from starlette.concurrency import run_in_threadpool

from app.api.v1.deps import require_api_key
from app.firestore_db.firebase_schemas import Businesses, Configurations
from app.schemas.api.business_schemas import (
    BotStatusRequest,
    BotStatusResponse,
    BusinessCreateRequest,
    BusinessResponse,
    BusinessUpdateRequest,
    KapsoCredentialsRequest,
    KapsoCredentialsResponse,
    PlanRequest,
    PlanResponse,
    WebhookUrlResponse,
)
from app.schemas.enum_schemas import BusinessStatus
from app.ai import vertical_registry
from app.services import plans, secrets, tenant_profile, tenants
from app.services.phones import normalize_phone
from app.services.firestore_services.firestore_service import FireStoreCRUDService

router = APIRouter(dependencies=[Depends(require_api_key)])
logger = logging.getLogger("admin-api")


@router.post("", response_model=BusinessResponse, status_code=status.HTTP_201_CREATED)
def create_business(payload: BusinessCreateRequest) -> BusinessResponse:
    if not vertical_registry.is_registered(payload.bussiness_vertical.value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"La vertical {payload.bussiness_vertical.value!r} todavía no tiene agente",
        )
    whatsapp_phone_number = _phone_or_400(payload.whatsapp_phone_number)
    _ensure_number_is_free(whatsapp_phone_number, payload.whatsapp_phone_number_id)

    now = datetime.now(timezone.utc)
    crud = FireStoreCRUDService()

    business = crud.create(
        Businesses,
        name=payload.name.strip(),
        legal_name=payload.legal_name.strip(),
        bussiness_vertical=payload.bussiness_vertical.value,
        phone=_phone_or_400(payload.phone),
        whatsapp_phone_number=whatsapp_phone_number,
        whatsapp_phone_number_id=payload.whatsapp_phone_number_id,
        whatsapp_provider=payload.whatsapp_provider,
        address=payload.address,
        status=BusinessStatus.ACTIVE.value,
        plan=payload.plan.value if payload.plan else None,
        webhook_tenant_key=tenants.new_tenant_key(payload.name),
        webhook_tenant_key_rotated_at=now,
        created_at=now,
        updated_at=now,
    )

    try:
        crud.create(
            Configurations,
            configuration_id=business.bussiness_id,
            business_id=business.bussiness_id,
            admin_phone_numbers=[],
        )
    except Exception:
        # Sin configuración el negocio no se puede administrar (GET da 404):
        # mejor que el alta falle entera y se reintente.
        crud.delete(business)
        raise

    return _business_response(business)


def _phone_or_400(phone: str) -> str:
    normalized = normalize_phone(phone)
    if len(normalized) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Teléfono inválido: {phone!r}")
    return normalized


def _ensure_number_is_free(whatsapp_phone_number: str, phone_number_id: str, business_id: str | None = None) -> None:
    """409 si otro negocio ya tiene ese número o ese phone_number_id: los
    eventos de WhatsApp se asignan al negocio por esos dos datos."""
    for field, value in (("whatsapp_phone_number", whatsapp_phone_number), ("whatsapp_phone_number_id", phone_number_id)):
        other = Businesses.collection.filter(**{field: value}).get()
        if other is not None and other.bussiness_id != business_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Ese {field} ya es del negocio {other.bussiness_id}")


@router.get("", response_model=list[BusinessResponse])
def list_businesses() -> list[BusinessResponse]:
    # ponytail: lee todos los negocios; paginar si pasan de unos cientos.
    FireStoreCRUDService()  # conecta fireo
    return sorted((_business_response(b) for b in Businesses.collection.fetch()), key=lambda b: b.name.lower())


def _business_response(business: Businesses) -> BusinessResponse:
    data = business.to_dict()
    # Negocios viejos sin estos datos: que el listado no reviente por uno.
    for field in ("name", "legal_name", "bussiness_vertical", "phone", "whatsapp_phone_number"):
        data[field] = data.get(field) or ""
    data["status"] = data.get("status") or BusinessStatus.ACTIVE.value
    data["plan"] = plans.plan_of(business).value
    data["features"] = sorted(plans.features_of(business))
    return BusinessResponse(**data)


@router.get("/{business_id}", response_model=BusinessResponse)
def get_business(business_id: str) -> BusinessResponse:
    return _business_response(_get_business_or_404(business_id))


@router.patch("/{business_id}", response_model=BusinessResponse)
def update_business(business_id: str, payload: BusinessUpdateRequest) -> BusinessResponse:
    business = _get_business_or_404(business_id)
    updates = payload.model_dump(exclude_unset=True)
    for field in ("phone", "whatsapp_phone_number"):
        if updates.get(field):
            updates[field] = _phone_or_400(updates[field])
    for field in ("name", "legal_name", "whatsapp_phone_number", "whatsapp_phone_number_id"):
        if field in updates and not updates[field]:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field} no puede quedar vacío")
    _ensure_number_is_free(
        updates.get("whatsapp_phone_number", business.whatsapp_phone_number),
        updates.get("whatsapp_phone_number_id", business.whatsapp_phone_number_id),
        business_id,
    )
    for field, value in updates.items():
        setattr(business, field, value.strip() if isinstance(value, str) else value)
    business.updated_at = datetime.now(timezone.utc)
    FireStoreCRUDService().update(business)
    # El nombre entra al prompt. El webhook (otro servicio) ve un
    # phone_number_id nuevo cuando vence su caché de 60 s.
    tenant_profile.invalidate(business_id)
    return _business_response(business)


def _get_business_or_404(business_id: str) -> Businesses:
    business = FireStoreCRUDService().get(Businesses, business_id)
    if business is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Negocio no encontrado")
    return business


def _credentials_status(business: Businesses) -> KapsoCredentialsResponse:
    return KapsoCredentialsResponse(
        api_key_configured=bool(business.kapso_api_key_secret),
        webhook_secret_configured=bool(business.kapso_webhook_secret_secret),
    )


@router.get("/{business_id}/kapso-credentials", response_model=KapsoCredentialsResponse)
def get_kapso_credentials(business_id: str) -> KapsoCredentialsResponse:
    """Solo dice si están configuradas; los valores nunca salen."""
    return _credentials_status(_get_business_or_404(business_id))


@router.put("/{business_id}/kapso-credentials", response_model=KapsoCredentialsResponse)
async def put_kapso_credentials(business_id: str, payload: KapsoCredentialsRequest) -> KapsoCredentialsResponse:
    """Crea o rota (versión nueva) los secretos de Kapso del negocio en
    Secret Manager y guarda en Firestore solo sus referencias."""
    # campo de Businesses -> (tipo de secreto, valor)
    updates = {
        "kapso_api_key_secret": (secrets.KAPSO_API_KEY, (payload.api_key or "").strip()),
        "kapso_webhook_secret_secret": (secrets.KAPSO_WEBHOOK_SECRET, (payload.webhook_secret or "").strip()),
    }
    updates = {field: kv for field, kv in updates.items() if kv[1]}
    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Envía api_key y/o webhook_secret")
    if any(len(value) < 16 for _, value in updates.values()):
        # Un HMAC con un secreto corto se adivina. El detalle no repite el valor.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cada valor debe tener al menos 16 caracteres")

    business = await run_in_threadpool(_get_business_or_404, business_id)
    for field, (kind, value) in updates.items():
        setattr(business, field, await run_in_threadpool(secrets.put_secret, kind, business_id, value))
    business.updated_at = datetime.now(timezone.utc)
    await run_in_threadpool(FireStoreCRUDService().update, business)
    return _credentials_status(business)


def _bot_status_response(business: Businesses) -> BotStatusResponse:
    return BotStatusResponse(
        status=business.status or BusinessStatus.ACTIVE.value,
        changed_at=business.bot_status_changed_at,
        changed_by=business.bot_status_changed_by,
        reason=business.bot_status_reason,
    )


@router.get("/{business_id}/bot-status", response_model=BotStatusResponse)
def get_bot_status(business_id: str) -> BotStatusResponse:
    return _bot_status_response(_get_business_or_404(business_id))


@router.put("/{business_id}/bot-status", response_model=BotStatusResponse)
def put_bot_status(business_id: str, payload: BotStatusRequest) -> BotStatusResponse:
    """Kill switch del negocio. "inactive": los mensajes se guardan sin
    pasar por la IA; al volver a "active" solo se contestan los nuevos."""
    business = _get_business_or_404(business_id)
    now = datetime.now(timezone.utc)
    logger.warning(
        "Bot del negocio %s: %s -> %s por %s (%s)",
        business_id,
        business.status,
        payload.status,
        payload.actor,
        payload.reason or "sin motivo",
        extra={"fields": {"ops_event": "bot_status_changed", "business_id": business_id}},
    )
    business.status = payload.status
    business.bot_status_changed_at = now
    business.bot_status_changed_by = payload.actor.strip()
    business.bot_status_reason = payload.reason
    business.updated_at = now
    FireStoreCRUDService().update(business)
    return _bot_status_response(business)


def _plan_response(business: Businesses) -> PlanResponse:
    return PlanResponse(
        plan=plans.plan_of(business).value,
        features=sorted(plans.features_of(business)),
        changed_at=business.plan_changed_at,
        changed_by=business.plan_changed_by,
    )


@router.get("/{business_id}/plan", response_model=PlanResponse)
def get_plan(business_id: str) -> PlanResponse:
    return _plan_response(_get_business_or_404(business_id))


@router.put("/{business_id}/plan", response_model=PlanResponse)
def put_plan(business_id: str, payload: PlanRequest) -> PlanResponse:
    """Cambia el plan comercial. Aplica de inmediato: cada función consulta
    el plan al usarse (ver app/services/plans.py)."""
    business = _get_business_or_404(business_id)
    now = datetime.now(timezone.utc)
    logger.warning(
        "Plan del negocio %s: %s -> %s por %s",
        business_id,
        plans.plan_of(business).value,
        payload.plan.value,
        payload.actor,
        extra={"fields": {"ops_event": "plan_changed", "business_id": business_id}},
    )
    business.plan = payload.plan.value
    business.plan_changed_at = now
    business.plan_changed_by = payload.actor.strip()
    business.updated_at = now
    FireStoreCRUDService().update(business)
    return _plan_response(business)


def _webhook_url_response(business: Businesses) -> WebhookUrlResponse:
    key = business.webhook_tenant_key
    return WebhookUrlResponse(
        url=tenants.webhook_url(key) if key else None,
        rotated_at=business.webhook_tenant_key_rotated_at,
    )


@router.get("/{business_id}/webhook-url", response_model=WebhookUrlResponse)
def get_webhook_url(business_id: str) -> WebhookUrlResponse:
    """URL a registrar en Kapso para este negocio. `url` es null en los
    negocios creados antes del webhook por tenant: se genera con rotate."""
    return _webhook_url_response(_get_business_or_404(business_id))


@router.post("/{business_id}/webhook-url/rotate", response_model=WebhookUrlResponse)
def rotate_webhook_url(business_id: str) -> WebhookUrlResponse:
    """Genera una clave nueva. La anterior deja de servir en cuanto vence la
    caché del webhook (hasta 60 s); hay que actualizar la URL en Kapso."""
    business = _get_business_or_404(business_id)
    now = datetime.now(timezone.utc)
    business.webhook_tenant_key = tenants.new_tenant_key(business.name)
    business.webhook_tenant_key_rotated_at = now
    business.updated_at = now
    FireStoreCRUDService().update(business)
    return _webhook_url_response(business)
