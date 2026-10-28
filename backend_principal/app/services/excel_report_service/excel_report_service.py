"""Genera el Excel del negocio a partir de los pedidos de Firestore.

Hay un solo Excel: la plantilla `Plantillas Excel/Plantilla_Dashboard.xlsx`
(diseño, tablas dinámicas y gráficos hechos a mano en Excel) con los
pedidos (`Orders`) del rango pegados en su tabla `datos`. Ver
dashboard_report.py; firestore_repository.py hace las consultas.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.excel_report_service import dashboard_report
from app.services.excel_report_service.firestore_repository import FirestoreReportRepository
from app.services.excel_report_service.models import ExcelReportSpec

__all__ = ["ExcelReportService", "ExcelReportSpec"]


class ExcelReportService:
    def __init__(
        self,
        credentials: Any = None,
        connect: bool = True,
    ) -> None:
        self._repo = FirestoreReportRepository(credentials, connect)

    def generate_report(self, spec: ExcelReportSpec) -> Path:
        """Todo Excel sale de la plantilla: los pedidos (Orders) del rango
        se pegan en su tabla `datos`, sea cual sea el report_type."""
        return dashboard_report.generate_dashboard_report(spec, self._repo)
