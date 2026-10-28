"""Aplica una corrección en texto libre a un menú ya extraído (borrador
pendiente_confirmacion, esquema menu_del_dia/otras_opciones), vía el mismo
modelo -- acá sin imagen, solo texto: JSON actual + instrucción -> JSON
actualizado. No pasa por el admin_agent conversacional por la misma razón
que vision_extractor: acá no hace falta un agente con tools, solo
transformar un JSON.
"""

import json
import logging

from google.genai import types

from app.ai.core.genai_client import build_genai_client
from app.config import ai_restaurant_settings
from app.services.daily_menu.vision_extractor import ExtractedMenu

logger = logging.getLogger("whatsapp-webhook")


class MenuCorrectionError(Exception):
    """La corrección no se pudo aplicar (el modelo no devolvió JSON válido)."""


_CORRECTION_INSTRUCTIONS = """
Eres un sistema de edición de datos, no un asistente conversacional. Te doy
el JSON del menú del día de un restaurante (esquema menu_del_dia/
acompanamientos/otras_opciones) y una instrucción en español de un
administrador pidiendo un cambio puntual. Aplica SOLO el cambio pedido y
devuelve el JSON COMPLETO actualizado (con el mismo esquema), no un parche
ni un resumen.

Ejemplos de instrucciones típicas y cómo aplicarlas:
- "el menú del día vale 19.000" -> menu_del_dia.precio = 19000,
  menu_del_dia.tipo_precio = "fijo".
- "otras opciones a 21.000" -> otras_opciones.precio = 21000,
  otras_opciones.tipo_precio = "fijo".
- "hoy la sopa es solo ajiaco" -> en acompanamientos, busca el que se llama
  "Sopa" (sin importar mayúsculas) y deja opciones = ["Ajiaco"] (ya no es
  una elección, es la única).
- "hoy no hay chicharrón" -> quita "Chicharrón" (sin importar mayúsculas ni
  tilde) de otras_opciones.proteinas.
- "agrega jugo en vez de bebida" -> en acompanamientos, busca el que se
  llama "Bebida" y cámbiale el nombre a "Jugo" (mantén sus opciones tal
  cual, vacías si no tenía).

Reglas generales:
- No inventes platos, proteínas, acompañamientos ni precios que no estén ya
  en el JSON o que el admin no haya dado explícitamente en su instrucción.
- Si la instrucción no tiene relación con ningún campo existente y no es
  claramente "agregar/quitar algo", deja el JSON igual y agrega en
  advertencias una nota describiendo qué no se entendió.
""".strip()


def apply_correction(current_menu: ExtractedMenu, correction_text: str) -> ExtractedMenu:
    client = build_genai_client()

    parts = [
        types.Part.from_text(
            text=(
                f"{_CORRECTION_INSTRUCTIONS}\n\n"
                f"JSON actual:\n{current_menu.model_dump_json()}\n\n"
                f"Instrucción del admin:\n{correction_text}"
            )
        )
    ]

    try:
        response = client.models.generate_content(
            model=ai_restaurant_settings.MODEL_RESTAURANT,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ExtractedMenu,
                temperature=0.0,
            ),
        )
        data = json.loads(response.text)
        return ExtractedMenu.model_validate(data)
    except Exception as e:
        logger.warning("No se pudo aplicar la corrección %r: %s", correction_text, e)
        raise MenuCorrectionError(str(e)) from e
