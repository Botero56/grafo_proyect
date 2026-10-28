"""Cliente para mandar texto a la Raspberry / impresora de UN negocio."""

import logging
from typing import Optional

import httpx

logger = logging.getLogger("whatsapp-webhook")


class PrinterClient:
    def __init__(
        self,
        print_url: str,
        token: Optional[str] = None,
        business_id: Optional[str] = None,
        timeout: float = 15,
        copies: int = 1,
        paper_width: Optional[str] = None,
    ) -> None:
        self._print_url = print_url
        self._token = token
        self._business_id = business_id
        self._timeout = timeout
        self._copies = copies
        self._paper_width = paper_width

    async def print_text(self, text: str, payment_method: Optional[str] = None) -> bool:
        """`payment_method` viaja como campo aparte, sin tocar `text`: el
        servidor de la Raspberry decide cómo mostrarlo en el ticket.

        Autenticación (contrato en docs/RUNBOOK.md): `Authorization: Bearer
        <token del negocio>` y `X-Business-Id`. La Raspberry compara el token
        en tiempo constante y rechaza un business_id que no es el suyo."""
        payload = {"text": text}
        if payment_method:
            payload["payment_method"] = payment_method
        if self._copies > 1:
            payload["copies"] = self._copies
        if self._paper_width:
            payload["paper_width"] = self._paper_width
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._business_id:
            headers["X-Business-Id"] = self._business_id
        try:
            # Sin seguir redirecciones (default de httpx): el token no viaja a otro host.
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._print_url, json=payload, headers=headers)
                response.raise_for_status()
                logger.info("🖨️ Enviado a imprimir correctamente (negocio %s)", self._business_id)
                return True
        except Exception as e:
            logger.error("❌ Error al imprimir (negocio %s): %s", self._business_id, e)
            return False
