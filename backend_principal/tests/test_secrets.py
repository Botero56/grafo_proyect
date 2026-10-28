"""Secretos por negocio: caché con TTL, versión anterior para rotar,
invalidación y que ningún valor termine en los logs."""

import logging
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import AlreadyExists, FailedPrecondition, NotFound

from app.config import secrets_settings
from app.services import secrets

REF = "projects/p/secrets/kapso-api-key--biz1"


class FakeSecretManager:
    def __init__(self, versions: dict[str, list[str]]):
        self.versions = versions  # secret_id -> [v1, v2, ...]
        self.reads = 0
        self.disabled: set[str] = set()  # "secret_id/numero"
        self.timeouts: list = []

    def access_secret_version(self, name, timeout=None):
        self.reads += 1
        self.timeouts.append(timeout)
        secret, _, version = name.rpartition("/versions/")
        secret_id = secret.rsplit("/", 1)[-1]
        values = self.versions.get(secret_id)
        if not values:
            raise NotFound("no existe")
        number = len(values) if version == "latest" else int(version)
        if f"{secret_id}/{number}" in self.disabled:
            raise FailedPrecondition("deshabilitada")
        return SimpleNamespace(
            name=f"{secret}/versions/{number}",
            payload=SimpleNamespace(data=values[number - 1].encode()),
        )

    def create_secret(self, request):
        if request["secret_id"] in self.versions:
            raise AlreadyExists("ya existe")
        self.versions[request["secret_id"]] = []

    def add_secret_version(self, request):
        self.versions[request["parent"].rsplit("/", 1)[-1]].append(request["payload"]["data"].decode())


@pytest.fixture
def manager(monkeypatch):
    fake = FakeSecretManager({"kapso-api-key--biz1": ["vieja", "nueva"]})
    monkeypatch.setattr(secrets, "_client", fake)
    monkeypatch.setattr(secrets_settings, "SECRETS_PROJECT", "p")
    secrets.clear_cache()
    yield fake
    secrets.clear_cache()


def test_reads_latest_and_previous_and_caches_them(manager):
    assert secrets.get_secret(REF) == "nueva"
    assert manager.reads == 1  # enviar no necesita la anterior
    assert secrets.get_secret_versions(REF) == ["nueva", "vieja"]
    assert secrets.get_secret_versions(REF) == ["nueva", "vieja"]
    assert manager.reads == 3  # latest + anterior, una sola vez
    assert all(t is not None for t in manager.timeouts)  # nunca el default de 60 s


def test_disabled_previous_version_no_longer_counts(manager):
    manager.disabled.add("kapso-api-key--biz1/1")
    assert secrets.get_secret_versions(REF) == ["nueva"]


def test_cache_expires_after_the_ttl(manager, monkeypatch):
    monkeypatch.setattr(secrets_settings, "SECRETS_CACHE_SECONDS", -1)  # vence apenas se guarda
    secrets.get_secret(REF)
    secrets.get_secret(REF)
    assert manager.reads == 2


def test_invalidate_picks_up_a_rotation(manager):
    assert secrets.get_secret(REF) == "nueva"
    manager.versions["kapso-api-key--biz1"].append("rotada")
    assert secrets.get_secret(REF) == "nueva"  # cacheado
    secrets.invalidate(REF)
    assert secrets.get_secret(REF) == "rotada"


def test_put_secret_creates_once_and_returns_only_the_reference(manager, caplog):
    caplog.set_level(logging.DEBUG)

    ref = secrets.put_secret(secrets.KAPSO_WEBHOOK_SECRET, "biz2", "s3cr3t-valor")
    secrets.put_secret(secrets.KAPSO_WEBHOOK_SECRET, "biz2", "s3cr3t-otro")

    assert ref == "projects/p/secrets/kapso-webhook-secret--biz2"
    assert secrets.get_secret_versions(ref) == ["s3cr3t-otro", "s3cr3t-valor"]
    assert "s3cr3t" not in caplog.text


def test_missing_secret_returns_none_without_leaking(manager, caplog):
    assert secrets.get_secret_or_none("projects/p/secrets/kapso-api-key--nadie") is None
    assert secrets.get_secret_or_none(None) is None
    assert "nueva" not in caplog.text and "vieja" not in caplog.text
