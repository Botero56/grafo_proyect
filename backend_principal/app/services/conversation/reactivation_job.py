"""
Barrido de reactivación automática del bot.

Recorre conversaciones pausadas (`bot_activo=False`) y reactiva las que
cumplen alguna de las dos salidas automáticas requeridas:
  (a) `pausado_hasta` ya venció, o
  (b) no hubo un mensaje humano en los últimos
      `HandoffSettings.HANDOFF_INACTIVITY_MINUTES` minutos.

Pensado para correr periódicamente vía Cloud Scheduler contra
`POST /api/v1/admin/jobs/reactivate-bots` (ver
app/api/v1/admin/jobs_router.py) -- no se implementó como loop dentro del
proceso porque Cloud Run puede reciclar la instancia entre requests. La
API admin vive en el worker (app/services/worker_entry.py).
"""

from datetime import datetime, timedelta, timezone

from app.config import handoff_settings
from app.firestore_db.firebase_schemas import Conversations
from app.services.conversation.handoff_service import HandoffService
from app.services.time_utils import as_utc


def run_reactivation_sweep(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    inactivity_cutoff = now - timedelta(minutes=handoff_settings.HANDOFF_INACTIVITY_MINUTES)

    # list(): .fetch() devuelve un QueryIterator de fireo (sin __len__ ni
    # __bool__, se agota al recorrerlo una vez) -- se materializa acá
    # porque abajo se recorre y también se le hace len().
    paused = list(Conversations.collection.filter(bot_activo=False).fetch())

    handoff_service = HandoffService()
    reactivated_ids: list[str] = []

    for conversation in paused:
        expired_by_deadline = (
            conversation.pausado_hasta is not None and as_utc(conversation.pausado_hasta) <= now
        )
        # Si nunca hubo un echo humano (ej. se pausó por la IA y nadie
        # respondió todavía), usamos pausado_desde como referencia de
        # inactividad para no dejarla pausada para siempre.
        last_human_at = HandoffService.pause_reference(conversation)
        expired_by_inactivity = (
            last_human_at is not None and as_utc(last_human_at) <= inactivity_cutoff
        )

        if expired_by_deadline or expired_by_inactivity:
            handoff_service.reactivate_bot(conversation)
            reactivated_ids.append(conversation.conversation_id)

    return {
        "checked": len(paused),
        "reactivated": len(reactivated_ids),
        "reactivated_ids": reactivated_ids,
    }
