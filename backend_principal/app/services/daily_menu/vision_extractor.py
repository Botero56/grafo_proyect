"""Lectura de UNA foto del admin, con visión: el menú del día y/o los
productos de carta, en una sola llamada al modelo.

Formato REAL del negocio (siempre el mismo, ver ejemplo en
_EXTRACTION_INSTRUCTIONS): un "menú del día" que cambia a diario, unas
"otras opciones" (proteína a elegir, casi siempre las mismas) que comparten
los mismos acompañamientos que el menú del día, y datos fijos de envíos/
contacto. No hay variedad de categorías arbitrarias -- por eso el esquema
es fijo (menu_del_dia/acompanamientos/otras_opciones), no una lista
genérica de categorías/productos.

Usa el mismo modelo ya configurado para el restaurante
(ai_restaurant_settings.MODEL_RESTAURANT, multimodal) vía un llamado directo
a google.genai -- no pasa por el admin_agent de ADK porque acá no hace falta
un agente conversacional con tools, solo "imagen + instrucciones -> JSON".
Reutiliza la misma configuración de Vertex AI que ya usa google-adk (ver
app/config.py: VertexAISettings).
"""

import json
import logging
from typing import Optional

from google import genai
from google.genai import types
from pydantic import AliasChoices, BaseModel, Field

from app.ai.core.genai_client import build_genai_client
from app.config import ai_restaurant_settings

logger = logging.getLogger("whatsapp-webhook")


class Acompanamiento(BaseModel):
    nombre: str
    # Vacío = sin alternativas (va tal cual, ej. "Arroz"). Con alternativas
    # (ej. "Sopa de ajiaco o frijoles") -> ["Ajiaco", "Frijoles"].
    opciones: list[str] = Field(default_factory=list)


class MenuDelDia(BaseModel):
    """El "menú del día" propiamente dicho: el plato fijo que cambia a
    diario. Es a lo que se refiere un cliente cuando pregunta "¿cuál es el
    menú?" o "¿qué hay de menú del día?" -- por eso el campo se llama igual
    (`ExtractedMenu.menu_del_dia`, más abajo) en vez de "plato_del_dia": el
    nombre del campo es la señal que usan los agentes de IA (cliente y
    extracción) para asociar la pregunta con este dato sin tener que
    inferirlo. "Otras opciones" es el segundo producto pedible, distinto de
    este."""

    nombre: str
    precio: Optional[float] = None
    tipo_precio: str  # "fijo" | "no_legible"


class OtrasOpciones(BaseModel):
    proteinas: list[str] = Field(default_factory=list)
    precio: Optional[float] = None
    tipo_precio: str  # "fijo" | "no_legible"


class ExtractedMenu(BaseModel):
    # validation_alias acepta también la clave vieja "plato_del_dia" para
    # poder seguir leyendo documentos ya guardados en Firestore con el
    # nombre de campo anterior (renombrado 2026-09-23, schema_version 2 ->
    # 3). Los JSON nuevos (extracción, correcciones, model_dump_json) usan
    # el nombre de campo real, "menu_del_dia" -- ver MenuDelDia arriba.
    menu_del_dia: MenuDelDia = Field(
        validation_alias=AliasChoices("menu_del_dia", "plato_del_dia")
    )
    # Los acompañamientos van a nivel de menú porque aplican a AMBAS
    # opciones (menú del día y otras opciones), no a una en particular.
    acompanamientos: list[Acompanamiento] = Field(default_factory=list)
    otras_opciones: OtrasOpciones
    envios: Optional[bool] = None
    telefono_contacto: Optional[str] = None
    fecha_detectada: Optional[str] = None
    advertencias: list[str] = Field(default_factory=list)
    confianza: float = 0.0

    def parece_menu(self) -> bool:
        """False cuando el modelo no encontró ni menú del día ni
        proteínas -- señal de que la imagen no era un menú (ver
        orchestrator.py)."""
        return bool(self.menu_del_dia.nombre) or bool(self.otras_opciones.proteinas)


class CartaItem(BaseModel):
    categoria: str
    nombre: str
    precio: Optional[float] = None
    descripcion: Optional[str] = None


class ExtractedUpload(BaseModel):
    """Lo que trae UNA foto del admin, leído en una sola llamada: el menú
    del día (vacío si la foto no lo tiene) y los productos de carta."""

    menu: ExtractedMenu
    productos: list[CartaItem] = Field(default_factory=list)


class MenuExtractionError(Exception):
    """La imagen no pudo extraerse como menú tras los reintentos."""


_EXTRACTION_INSTRUCTIONS = """
Eres un sistema de extracción de datos, no un asistente conversacional.
Vas a recibir la foto de un menú de restaurante. Este negocio SIEMPRE usa
el mismo formato de menú, con tres partes:

1. "MENÚ DEL DÍA" (cambia a diario): un nombre de plato, con
   acompañamientos incluidos (algunos con elección, ej. "sopa de ajiaco o
   frijoles") y un precio. Esto es lo que la mayoría de clientes quiere
   decir cuando pregunta "¿cuál es el menú?" o "¿qué hay de menú hoy?".
2. "OTRAS OPCIONES" (casi siempre igual día a día): una proteína a elegir
   entre varias (ej. cerdo, chicharrón, pollo, res), con un único precio
   para cualquiera de ellas. Incluye LOS MISMOS acompañamientos del menú
   del día (no los repitas en la imagen, van en la misma lista
   "acompanamientos").
3. Datos fijos: si hacen domicilios/envíos, y el teléfono de contacto.

Ejemplo real (así se ve siempre):
  Menú del día: "Pollo al curry con champiñones" -- incluye sopa (ajiaco o
  frijoles), torta de queso, arroz, ensalada, bebida -- $18.500.
  Otras opciones: cerdo, chicharrón, pollo o res -- mismos acompañamientos
  -- $20.500.
  Envíos: sí. Contacto: 3235099092.

  Eso se extrae como:
  {
    "menu_del_dia": {"nombre": "Pollo al curry con champiñones", "precio": 18500, "tipo_precio": "fijo"},
    "acompanamientos": [
      {"nombre": "Sopa", "opciones": ["Ajiaco", "Frijoles"]},
      {"nombre": "Torta de queso", "opciones": []},
      {"nombre": "Arroz", "opciones": []},
      {"nombre": "Ensalada", "opciones": []},
      {"nombre": "Bebida", "opciones": []}
    ],
    "otras_opciones": {"proteinas": ["Cerdo", "Chicharrón", "Pollo", "Res"], "precio": 20500, "tipo_precio": "fijo"},
    "envios": true,
    "telefono_contacto": "3235099092",
    "fecha_detectada": null,
    "advertencias": [],
    "confianza": 0.95
  }

Reglas estrictas:
- Ignora la decoración de la imagen (fotos de comida, logo, íconos): extrae
  SOLO el texto del menú.
- NUNCA inventes platos, proteínas, acompañamientos ni precios que no estén
  en la imagen.
- Si un precio no se alcanza a leer con certeza, tipo_precio = "no_legible"
  y precio = null. Este formato no tiene precios "según tamaño" ni
  variables -- si no es un número claro, es no_legible.
- Si el precio es un número claro, tipo_precio = "fijo" y precio es ese
  número normalizado (ej. "18.000" o "18,500" -> 18500, sin separadores de
  miles).
- "Sopa de ajiaco o frijoles" (con alternativas) -> {"nombre": "Sopa",
  "opciones": ["Ajiaco", "Frijoles"]}. Un acompañamiento sin alternativas
  (ej. "Arroz") -> opciones vacío.
- envios: true si la imagen menciona domicilios/envíos, false si dice
  explícitamente que no hay, null si no se menciona.
- telefono_contacto: el número de contacto tal como aparece, o null si no
  hay ninguno visible.
- Si la imagen muestra una fecha o día de la semana, ponlo tal cual en
  fecha_detectada; si no, null.
- Si la imagen NO tiene un menú del día con este formato, deja
  menu_del_dia.nombre = "" y otras_opciones.proteinas = [].

Todo lo anterior va en "menu". Además, en "productos" va la CARTA: los
productos con precio por sección (ej. "Bebidas", "Asados", "Adiciones")
que NO son parte del menú del día ni de otras opciones. Una foto puede
tener solo menú del día, solo carta, o las dos cosas.
- Cada producto con su sección en "categoria" y su precio normalizado
  ("$ 8.000" -> 8000; si no se lee con certeza, precio = null).
- Si varios sabores o variantes comparten precio y cada uno se pide aparte
  (ej. "Limonadas $10.000: cereza, coco"), crea un producto por cada uno
  ("Limonada de cereza", "Limonada de coco").
- Si los sabores se eligen dentro de un mismo producto (ej. "Jugos en agua
  $8.000 / en leche $9.000" con sabores maracuyá, mora...), crea un producto
  por precio ("Jugo en agua", "Jugo en leche") y pon los sabores en
  descripcion ("Sabores: maracuyá, mora").
- NUNCA repitas en "productos" el menú del día ni las proteínas de otras
  opciones, ni inventes productos o precios.
- Si la imagen no es ni un menú ni una carta (foto random, factura, etc.),
  deja el menú vacío como se dijo arriba y productos = [].
""".strip()


def _extract_once(
    client: genai.Client,
    image_bytes: bytes,
    mime_type: str,
    caption: Optional[str],
    previous_menu: Optional[ExtractedMenu],
) -> ExtractedUpload:
    parts = [
        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
        types.Part.from_text(text=_EXTRACTION_INSTRUCTIONS),
    ]
    if caption:
        parts.append(
            types.Part.from_text(
                text=f"Contexto adicional escrito por el admin al mandar la foto: {caption}"
            )
        )
    if previous_menu is not None:
        # Referencia de formato/nombres (ej. cómo se llamó "Sopa" ayer), NO
        # para copiar precios ni proteínas -- eso se lee solo de la imagen
        # de hoy. La comparación real de cambios la hace menu_diff.py
        # después, sobre el resultado ya extraído.
        parts.append(
            types.Part.from_text(
                text=(
                    "Referencia: así se veía el menú de este negocio el día anterior "
                    "(úsalo solo para entender el formato esperado, la imagen de HOY "
                    f"manda sobre cualquier dato):\n{previous_menu.model_dump_json()}"
                )
            )
        )

    response = client.models.generate_content(
        model=ai_restaurant_settings.MODEL_RESTAURANT,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ExtractedUpload,
            temperature=0.0,
        ),
    )

    data = json.loads(response.text)
    return ExtractedUpload.model_validate(data)


def extract_upload_from_image(
    image_bytes: bytes,
    mime_type: str,
    caption: Optional[str] = None,
    previous_menu: Optional[ExtractedMenu] = None,
) -> ExtractedUpload:
    """Lee UNA foto del admin en una sola llamada: menú del día y carta.
    Reintenta una vez si el modelo no devuelve JSON válido contra el
    esquema; si vuelve a fallar, levanta MenuExtractionError (el
    orchestrator es quien le avisa al admin).

    `previous_menu`: el último menú ACTIVO del negocio (si existe), para
    dárselo al modelo como referencia de formato -- ver
    WhatsAppWebhookEventRouter / orchestrator.py.
    """
    client = build_genai_client()

    last_error: Optional[Exception] = None
    for attempt in range(2):
        try:
            return _extract_once(client, image_bytes, mime_type, caption, previous_menu)
        except Exception as e:
            last_error = e
            logger.warning("Extracción de menú falló (intento %s/2): %s", attempt + 1, e)

    raise MenuExtractionError(f"No se pudo extraer el menú tras 2 intentos: {last_error}")
