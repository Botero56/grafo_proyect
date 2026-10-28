"""API admin (`/api/v1/admin/*`), montada en el worker
(app/services/worker_entry.py).

El worker es privado: Cloud Run exige IAM/OIDC antes de llegar acá, y
además cada router pide la API key admin (`require_api_key`). Los jobs
(`/admin/jobs/*`) los dispara Cloud Scheduler con un token OIDC más la
cabecera X-API-Key.
"""

from fastapi import APIRouter

from app.api.v1.admin.businesses_router import router as businesses_router
from app.api.v1.admin.configurations_router import router as configurations_router
from app.api.v1.admin.conversations_router import router as conversations_router
from app.api.v1.admin.fixed_menu_items_router import router as fixed_menu_items_router
from app.api.v1.admin.jobs_router import router as jobs_router

api_router = APIRouter()

api_router.include_router(
    businesses_router, prefix="/admin/businesses", tags=["admin"]
)
api_router.include_router(
    configurations_router, prefix="/admin/configurations", tags=["admin"]
)
api_router.include_router(
    conversations_router, prefix="/admin/conversations", tags=["admin"]
)
api_router.include_router(
    jobs_router, prefix="/admin/jobs", tags=["admin", "jobs"]
)
api_router.include_router(
    fixed_menu_items_router, prefix="/admin/restaurant/fixed-menu-items", tags=["admin", "restaurant"]
)
