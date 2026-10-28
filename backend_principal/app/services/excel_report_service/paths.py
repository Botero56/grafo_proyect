"""Resolución de la ruta de salida por defecto para los reportes generados."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .constants import DEFAULT_TZ


def default_output_path(business_id: str, report_type: str) -> Path:
    stamp = datetime.now(DEFAULT_TZ).strftime("%Y%m%d_%H%M%S")
    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    return reports_dir / f"Dashboard_Almuerzos_{business_id}_{stamp}.xlsx"
