"""En Cloud Run cada log debe ser UNA línea JSON con severidad y con el
contexto de la conversación: sin eso un fallo en producción no se podía
diagnosticar (los ERROR salían como DEFAULT y sin saber de quién eran)."""

import asyncio
import json
import logging

from app import logging_setup


def _record(msg, level=logging.ERROR, exc_info=None):
    return logging.LogRecord("whatsapp-webhook", level, __file__, 1, msg, (), exc_info)


def test_json_line_has_severity_context_and_the_traceback():
    async def one_request():
        logging_setup.bind(wamid="wamid.1", conversation_id="conv-1", business_id=None)
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            import sys

            return logging_setup._JsonFormatter().format(_record("falló el envío", exc_info=sys.exc_info()))

    line = asyncio.run(one_request())

    entry = json.loads(line)
    assert entry["severity"] == "ERROR"
    assert entry["wamid"] == "wamid.1" and entry["conversation_id"] == "conv-1"
    assert "business_id" not in entry  # None no se agrega
    assert entry["message"].startswith("falló el envío\nTraceback")
    assert "\n" not in line  # una sola entrada en Cloud Logging


def test_context_does_not_leak_between_requests():
    async def request(conversation_id):
        logging_setup.bind(conversation_id=conversation_id)
        await asyncio.sleep(0)
        return json.loads(logging_setup._JsonFormatter().format(_record("x")))["conversation_id"]

    async def both():
        return await asyncio.gather(request("a"), request("b"))

    assert asyncio.run(both()) == ["a", "b"]


def test_metric_fields_go_into_the_json_line():
    record = _record("Turno de IA", level=logging.INFO)
    record.fields = {"outcome": "ok", "prompt_tokens": 8000}

    entry = json.loads(logging_setup._JsonFormatter().format(record))

    assert (entry["outcome"], entry["prompt_tokens"]) == ("ok", 8000)
