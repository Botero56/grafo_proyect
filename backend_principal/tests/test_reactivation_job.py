from datetime import datetime, timedelta, timezone

from app.config import handoff_settings
from app.firestore_db.firebase_schemas import Conversations
from app.services.conversation.reactivation_job import run_reactivation_sweep
from tests.conftest import seed_business, seed_conversation


def _paused_conversation(
    fake_store,
    business,
    customer_id: str,
    *,
    pausado_hasta=None,
    ultimo_mensaje_humano_at=None,
    pausado_desde=None,
) -> Conversations:
    return seed_conversation(
        fake_store,
        business,
        customer_id,
        bot_activo=False,
        motivo_handoff="respuesta_manual",
        pausado_desde=pausado_desde or datetime.now(timezone.utc),
        pausado_hasta=pausado_hasta,
        ultimo_mensaje_humano_at=ultimo_mensaje_humano_at,
    )


def test_reactivates_conversation_past_pausado_hasta(fake_store):
    business = seed_business(fake_store)
    now = datetime.now(timezone.utc)
    expired = _paused_conversation(
        fake_store, business, "c1", pausado_hasta=now - timedelta(minutes=1)
    )
    still_valid = _paused_conversation(
        fake_store, business, "c2", pausado_hasta=now + timedelta(hours=1)
    )

    result = run_reactivation_sweep(now=now)

    assert expired.conversation_id in result["reactivated_ids"]
    assert still_valid.conversation_id not in result["reactivated_ids"]
    assert expired.bot_activo is True
    assert still_valid.bot_activo is False


def test_reactivates_conversation_by_inactivity(fake_store):
    business = seed_business(fake_store)
    now = datetime.now(timezone.utc)
    cutoff_minutes = handoff_settings.HANDOFF_INACTIVITY_MINUTES

    inactive = _paused_conversation(
        fake_store,
        business,
        "c3",
        ultimo_mensaje_humano_at=now - timedelta(minutes=cutoff_minutes + 10),
    )
    recent = _paused_conversation(
        fake_store,
        business,
        "c4",
        ultimo_mensaje_humano_at=now - timedelta(minutes=5),
    )

    result = run_reactivation_sweep(now=now)

    assert inactive.conversation_id in result["reactivated_ids"]
    assert recent.conversation_id not in result["reactivated_ids"]


def test_active_conversations_are_not_touched(fake_store):
    business = seed_business(fake_store)
    seed_conversation(fake_store, business, "c5", bot_activo=True)

    result = run_reactivation_sweep(now=datetime.now(timezone.utc))

    assert result["checked"] == 0
    assert result["reactivated"] == 0


def test_naive_pausado_hasta_from_the_admin_api_does_not_break_the_sweep(fake_store):
    """PauseBotRequest acepta fechas sin zona; compararlas con `now` (UTC con
    zona) reventaba con TypeError y cortaba el barrido entero."""
    business = seed_business(fake_store)
    now = datetime.now(timezone.utc)
    naive_expired = (now - timedelta(minutes=1)).replace(tzinfo=None)
    expired = _paused_conversation(fake_store, business, "c6", pausado_hasta=naive_expired)

    result = run_reactivation_sweep(now=now)

    assert expired.conversation_id in result["reactivated_ids"]
