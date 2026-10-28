"""Endpoints pensados para dispararse desde un scheduler externo (Cloud
Scheduler en producción), no desde el navegador/admin humano. Reutilizan la
misma API key admin (`require_api_key`) -- ver decisión de diseño en
HANDOFF.md."""

from fastapi import APIRouter, Depends

from app.api.v1.deps import require_api_key
from app.schemas.api.conversation_schemas import ReactivationSweepResponse
from app.schemas.api.daily_menu_schemas import DailyMenuSweepResponse
from app.services.conversation.reactivation_job import run_reactivation_sweep
from app.services.delivery import tracking
from app.services.daily_menu.reminders import expire_pending_daily_menus, remind_missing_daily_menus
from app.services.messaging.unanswered_job import check_unanswered

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("/reactivate-bots", response_model=ReactivationSweepResponse)
def reactivate_bots() -> ReactivationSweepResponse:
    result = run_reactivation_sweep()
    return ReactivationSweepResponse(**result)


@router.post("/expire-pending-daily-menus", response_model=DailyMenuSweepResponse)
async def expire_pending_daily_menus_job() -> DailyMenuSweepResponse:
    result = await expire_pending_daily_menus()
    return DailyMenuSweepResponse(**result)


@router.post("/check-unanswered")
def check_unanswered_job() -> dict:
    """Cada 5 min desde Cloud Scheduler: loguea ERROR por cada conversación
    de un negocio abierto con mensajes sin contestar (ver unanswered_job)."""
    return check_unanswered()


@router.post("/delivery-sweep")
async def delivery_sweep_job() -> dict:
    """Cada 10 min desde Cloud Scheduler: presume entregados los pedidos que
    pasaron el tiempo estimado y hace el cierre diario de domicilios de los
    negocios que ya pasaron su hora de corte (ver delivery/tracking.py)."""
    return await tracking.sweep()


@router.post("/remind-missing-daily-menus", response_model=DailyMenuSweepResponse)
async def remind_missing_daily_menus_job() -> DailyMenuSweepResponse:
    result = await remind_missing_daily_menus()
    return DailyMenuSweepResponse(**result)
