"""POST /webhook/kapso/{tenantKey}: cada negocio con su URL y su secreto.
Lo que se protege: nada se encola sin la firma del secreto DE ESE negocio,
un tenant no puede inyectar eventos de otro número, y la rotación del
secreto no corta la entrega."""

import hashlib
import hmac
import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import whatsapp_settings
from app.services import secrets, tenants, webhook_entry

client = TestClient(webhook_entry.app)

KEY = "edygourmet_" + "a" * 32
OTHER_KEY = "otro_" + "b" * 32
TENANT = tenants.Tenant(business_id="b1", phone_number_id="111", webhook_secret_ref="projects/p/secrets/kapso-webhook-secret--b1")
SECRET = "secreto-nuevo-del-tenant-0001"
OLD_SECRET = "secreto-viejo-del-tenant-0001"


def _body(wamid="wamid.A", phone_number_id="111", sent_at=None):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"display_phone_number": "573000000000", "phone_number_id": phone_number_id},
                            "messages": [
                                {
                                    "id": wamid,
                                    "from": "573001234567",
                                    "timestamp": str(int(sent_at or time.time())),
                                    "type": "text",
                                    "text": {"body": "hola"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _post(body, key=KEY, secret=SECRET):
    raw = json.dumps(body).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(f"/webhook/kapso/{key}", content=raw, headers={"X-Webhook-Signature": signature})


@pytest.fixture
def env():
    lookups = []

    def lookup(key):
        lookups.append(key)
        return TENANT if key == KEY else None

    tenants.clear_cache()
    with patch.object(tenants, "_lookup", side_effect=lookup), patch.object(
        secrets, "get_secret_versions", return_value=[SECRET, OLD_SECRET]
    ), patch.object(webhook_entry.cloud_tasks_client, "enqueue_event", return_value=True) as enqueue:
        yield {"enqueue": enqueue, "lookups": lookups}
    tenants.clear_cache()


def test_valid_signature_enqueues_with_the_business_id(env):
    response = _post(_body())
    assert response.status_code == 200
    payload, wamid, business_id = env["enqueue"].call_args.args
    assert (wamid, business_id) == ("wamid.A", "b1")


def test_previous_secret_version_still_validates_during_rotation(env):
    assert _post(_body(), secret=OLD_SECRET).status_code == 200


def test_bad_signature_is_401_and_enqueues_nothing(env, caplog):
    response = _post(_body(), secret="secreto-de-otro-tenant-00001")
    assert response.status_code == 401
    env["enqueue"].assert_not_called()
    assert "bad_signature" in str([getattr(r, "fields", "") for r in caplog.records])


def test_unknown_tenant_key_is_404_and_cached(env):
    assert _post(_body(), key=OTHER_KEY).status_code == 404
    assert _post(_body(), key=OTHER_KEY).status_code == 404
    assert env["lookups"] == [OTHER_KEY]  # el fallo queda en caché
    env["enqueue"].assert_not_called()


def test_malformed_key_never_reaches_firestore(env):
    assert _post(_body(), key="../../admin").status_code == 404
    assert _post(_body(), key="corta").status_code == 404
    assert env["lookups"] == []


def test_event_from_another_tenants_number_is_403(env):
    response = _post(_body(phone_number_id="999"))
    assert response.status_code == 403
    env["enqueue"].assert_not_called()


def test_old_message_is_dropped_with_200(env):
    stale = time.time() - whatsapp_settings.WEBHOOK_MAX_EVENT_AGE_SECONDS - 60
    assert _post(_body(sent_at=stale)).status_code == 200
    env["enqueue"].assert_not_called()


def test_redelivery_is_enqueued_under_the_same_task_name(env):
    _post(_body("wamid.X"))
    _post(_body("wamid.X"))
    # Mismo wamid = mismo nombre de tarea: Cloud Tasks responde AlreadyExists.
    assert [c.args[1] for c in env["enqueue"].call_args_list] == ["wamid.X", "wamid.X"]


def test_tenant_without_webhook_secret_is_503(env):
    with patch.object(tenants, "_lookup", return_value=tenants.Tenant("b1", "111", None)):
        tenants.clear_cache()
        assert _post(_body()).status_code == 503
    env["enqueue"].assert_not_called()


def test_rotated_tenant_key_dies_after_the_cache_ttl(env, monkeypatch):
    assert _post(_body()).status_code == 200
    monkeypatch.setattr(tenants, "_lookup", lambda key: None)  # se rotó la clave
    assert _post(_body()).status_code == 200  # sigue en caché (hasta 60 s)
    tenants._cache[KEY] = (TENANT, 0)  # vence la caché
    assert _post(_body()).status_code == 404
