import hmac

from fastapi import Header, HTTPException, status

from app.config import admin_api_settings


async def require_api_key(x_api_key: str = Header(...)) -> None:
    expected = admin_api_settings.API_KEY
    # Sin API_KEY configurada (ej. el webhook, que no la tiene) nada pasa.
    if not expected or not hmac.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="API key inválida"
        )
