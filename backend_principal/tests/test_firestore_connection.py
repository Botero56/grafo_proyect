"""Cada servicio se instancia por request; si cada uno volviera a crear su
firestore.Client, cada mensaje pagaría ~2 s de resolución de credenciales
por instancia (medido 2026-09-30). Se protege que la conexión sea una sola
por proceso y que un fallo no quede cacheado."""

from unittest.mock import MagicMock

import pytest

from app.services.excel_report_service.firestore_repository import FirestoreReportRepository
from app.services.firestore_services import firestore_service
from app.services.firestore_services.firestore_webhook_service import WhatsAppWebhookService


@pytest.fixture
def fresh_connection(monkeypatch):
    client_cls = MagicMock()
    monkeypatch.setattr(firestore_service, "_connected", False)
    monkeypatch.setattr(firestore_service.firestore, "Client", client_cls)
    monkeypatch.setattr(firestore_service.fireo, "connection", MagicMock())
    return client_cls


def test_every_service_shares_a_single_connection(fresh_connection):
    firestore_service.FireStoreCRUDService()
    firestore_service.FireStoreCRUDService()
    WhatsAppWebhookService()
    FirestoreReportRepository()

    fresh_connection.assert_called_once()


def test_a_failed_connection_is_retried_on_the_next_use(fresh_connection):
    fresh_connection.side_effect = [RuntimeError("sin credenciales"), MagicMock()]

    firestore_service.FireStoreCRUDService()
    firestore_service.FireStoreCRUDService()
    firestore_service.FireStoreCRUDService()

    assert fresh_connection.call_count == 2
