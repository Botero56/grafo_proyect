"""Dataclasses usadas como contrato de entrada/salida entre los distintos
generadores de reportes de este paquete."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .constants import ReportType


@dataclass
class ExcelReportSpec:
    """Contrato que usará la IA cuando sepa qué pidió el cliente."""

    business_id: str
    report_type: ReportType = "daily_sales"
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    order_status: Optional[str] = None
    columns: Optional[list[str]] = None
    sheet_name: Optional[str] = None
    output_path: Optional[Path] = None
    include_summary: bool = True
    timezone: str = "America/Bogota"
