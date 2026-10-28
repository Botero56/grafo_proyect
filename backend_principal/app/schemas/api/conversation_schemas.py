from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ConversationResponse(BaseModel):
    conversation_id: str
    business_id: str
    customer_id: str
    status: str
    bot_activo: bool = True
    motivo_handoff: Optional[str] = None
    agente_asignado: Optional[str] = None
    pausado_desde: Optional[datetime] = None
    pausado_hasta: Optional[datetime] = None


class PauseBotRequest(BaseModel):
    motivo: str
    agente_asignado: Optional[str] = None
    pausado_hasta: Optional[datetime] = None


class ReactivationSweepResponse(BaseModel):
    checked: int
    reactivated: int
    reactivated_ids: list[str]
