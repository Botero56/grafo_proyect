"""request_human_handoff es el último recurso: solo pasa a una persona si
el cliente lo pidió o falta un dato que la IA no puede inventar."""

import pytest

from app.ai.ai_restaurant.user.tools.customer_tools import request_human_handoff
from tests.conftest import FakeToolContext


@pytest.mark.parametrize("motivo", ["solicitud_cliente", "precio_variable", "sin_datos"])
def test_real_reasons_register_the_handoff(motivo):
    ctx = FakeToolContext("biz-1", "cust-1")

    result = request_human_handoff(motivo, "Reclamo por un pedido incompleto.", ctx)

    assert result["requiere_humano"] is True
    assert (ctx.state["requiere_humano"], ctx.state["motivo_handoff"]) == (True, motivo)
    assert ctx.state["resumen_handoff"] == "Reclamo por un pedido incompleto."


@pytest.mark.parametrize("motivo", ["baja_confianza", "coordinar_pago", ""])
def test_doubt_or_any_other_reason_keeps_the_ai_attending(motivo):
    """Regresión (prueba real 2026-10-05): con "baja_confianza" la IA pasaba
    a un humano por un pago mixto que el sistema ya resuelve."""
    ctx = FakeToolContext("biz-1", "cust-1")

    result = request_human_handoff(motivo, "No sé qué responder.", ctx)

    assert result["requiere_humano"] is False
    assert "requiere_humano" not in ctx.state
