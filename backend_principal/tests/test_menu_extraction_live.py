"""Extracción real contra la imagen de ejemplo, llamando a Gemini de
verdad (sin mockear). NO corre en CI ni en `pytest` por defecto -- solo
cuando se pide explícitamente con RUN_LIVE_GEMINI_TESTS=1, porque hace una
llamada de red real, tarda varios segundos y su salida puede variar un
poco entre corridas (es un modelo, no una función pura).

Uso: RUN_LIVE_GEMINI_TESTS=1 uv run pytest tests/test_menu_extraction_live.py -q
"""

import json
import os
from pathlib import Path

import pytest

from app.services.daily_menu.vision_extractor import extract_upload_from_image

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_LIVE_GEMINI_TESTS"),
    reason="Llama a Gemini de verdad -- correr explícitamente con RUN_LIVE_GEMINI_TESTS=1",
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "menus"


def test_extracts_real_menu_image_close_to_expected():
    image_bytes = (FIXTURES_DIR / "menu_dia_ejemplo.png").read_bytes()
    expected = json.loads((FIXTURES_DIR / "menu_dia_ejemplo.expected.json").read_text(encoding="utf-8"))

    result = extract_upload_from_image(image_bytes, "image/png", caption=None).menu

    assert result.menu_del_dia.tipo_precio == "fijo"
    assert result.menu_del_dia.precio == expected["menu_del_dia"]["precio"]
    assert result.otras_opciones.precio == expected["otras_opciones"]["precio"]
    assert set(p.lower() for p in result.otras_opciones.proteinas) == {
        p.lower() for p in expected["otras_opciones"]["proteinas"]
    }
    assert result.telefono_contacto == expected["telefono_contacto"]
    assert result.envios == expected["envios"]
