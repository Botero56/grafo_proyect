"""Logging del proceso: una línea JSON por registro en Cloud Run, texto en local.

Sin esto Python no tenía ningún handler: INFO se perdía y WARNING/ERROR
salían como texto plano sin severidad (Cloud Logging los mostraba como
DEFAULT, los tracebacks partidos en varias entradas y sin forma de saber
de qué conversación eran).

Cloud Run convierte cada línea JSON de stdout en una entrada estructurada:
`severity` y `message` son los campos que entiende, el resto queda en
jsonPayload y se puede filtrar (ej. jsonPayload.conversation_id="...").

`bind()` agrega campos de correlación (wamid, business_id, conversation_id)
a todos los logs de la request en curso, incluidos los de código que corre
en run_in_threadpool (copia el contexto). Cada request de uvicorn es su
propia tarea de asyncio, así que lo que se bindea en una no se filtra a otra.

Solo stdlib: lo importa también el webhook liviano.
"""

import json
import logging
import os
import sys
from contextvars import ContextVar

_fields: ContextVar[dict] = ContextVar("log_fields", default={})


def bind(**fields) -> None:
    """Agrega campos de correlación a los logs de la request actual."""
    _fields.set({**_fields.get(), **{k: v for k, v in fields.items() if v is not None}})


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            # El traceback va dentro del mismo `message`: una sola entrada en
            # Cloud Logging, y Error Reporting lo agrupa solo.
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return json.dumps(
            {
                "severity": record.levelname,
                "message": message,
                "logger": record.name,
                **_fields.get(),
                # logger.info("...", extra={"fields": {...}}): métricas
                # filtrables en Cloud Logging (ej. jsonPayload.prompt_tokens).
                **getattr(record, "fields", {}),
            },
            default=str,
        )


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        fields = {**_fields.get(), **getattr(record, "fields", {})}
        return f"{text} {fields}" if fields else text


def configure_logging() -> None:
    """Idempotente. JSON si corre en Cloud Run (K_SERVICE), texto si no."""
    root = logging.getLogger()
    if getattr(root, "_j2ys_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _JsonFormatter()
        if os.getenv("K_SERVICE")
        else _TextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    # Librerías que en INFO loguean cada request: ruido, y httpx además
    # escribe las URLs de descarga de media de WhatsApp (con hash temporal).
    for noisy in ("httpx", "httpcore", "urllib3", "google_adk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # google_genai avisa en CADA generate_content directo que no usemos AFC
    # (no lo usamos): solo sus errores sirven.
    logging.getLogger("google_genai").setLevel(logging.ERROR)
    # Los `warnings` de librerías salían como texto plano en dos líneas; así
    # quedan como una entrada JSON con severity WARNING.
    logging.captureWarnings(True)
    root._j2ys_configured = True
