from fastapi import APIRouter, Depends, HTTPException, status

from app.api.v1.deps import require_api_key
from app.firestore_db.firebase_schemas import Conversations
from app.schemas.api.conversation_schemas import ConversationResponse, PauseBotRequest
from app.schemas.enum_schemas import ConversationStatus
from app.services.conversation.handoff_service import HandoffService
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery

router = APIRouter(dependencies=[Depends(require_api_key)])


def _get_conversation_or_404(business_id: str, conversation_id: str) -> Conversations:
    conversation = TenantScopedQuery.get_by_id_for_business(
        Conversations, business_id, conversation_id
    )
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversación no encontrada para ese negocio",
        )
    return conversation


@router.get("/{conversation_id}", response_model=ConversationResponse)
def get_conversation(conversation_id: str, business_id: str) -> ConversationResponse:
    conversation = _get_conversation_or_404(business_id, conversation_id)
    return ConversationResponse(**conversation.to_dict())


@router.post("/{conversation_id}/pause-bot", response_model=ConversationResponse)
def pause_bot(
    conversation_id: str, business_id: str, payload: PauseBotRequest
) -> ConversationResponse:
    """Pausa manual: útil para que un agente humano tome la conversación
    proactivamente, sin esperar a que el cliente escriba o a que la IA pida
    handoff."""
    conversation = _get_conversation_or_404(business_id, conversation_id)
    conversation = HandoffService().pause_bot(
        conversation,
        motivo=payload.motivo,
        agente_asignado=payload.agente_asignado,
        pausado_hasta=payload.pausado_hasta,
    )
    return ConversationResponse(**conversation.to_dict())


@router.post("/{conversation_id}/reactivate-bot", response_model=ConversationResponse)
def reactivate_bot(conversation_id: str, business_id: str) -> ConversationResponse:
    conversation = _get_conversation_or_404(business_id, conversation_id)
    conversation = HandoffService().reactivate_bot(conversation)
    return ConversationResponse(**conversation.to_dict())


@router.post("/{conversation_id}/close", response_model=ConversationResponse)
def close_conversation(conversation_id: str, business_id: str) -> ConversationResponse:
    """Cierra la conversación Y reactiva el bot: la próxima vez que el
    cliente escriba, `_get_or_create_conversation` abre una conversación
    nueva con bot_activo=True por defecto."""
    conversation = _get_conversation_or_404(business_id, conversation_id)
    conversation.status = ConversationStatus.CLOSED.value
    # reactivate_bot guarda la conversación completa, status incluido.
    conversation = HandoffService().reactivate_bot(conversation)
    return ConversationResponse(**conversation.to_dict())
