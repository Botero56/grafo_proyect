"""Suite de evaluación con el modelo REAL (Vertex AI): conversaciones de
prueba con aserciones sobre el estado y la media enviada.

No corre con `pytest` normal (ver addopts en pyproject.toml). Antes de cada
despliegue:

    MODEL_RESTAURANT=<modelo de producción> uv run pytest -m eval tests/evals

El modelo debe ser el mismo del worker en producción (una variable de
entorno le gana al .env local, que puede tener otro). Necesita credenciales de Google (`gcloud auth application-default login`) y
el .env del proyecto. Firestore y WhatsApp son simulados: no toca datos ni
envía mensajes reales.
"""

import os
from pathlib import Path


def _load_dotenv() -> None:
    # google-genai (que usa ADK) lee la config de Vertex de os.environ, no del
    # .env que lee pydantic-settings.
    env = Path(__file__).resolve().parents[2] / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


_load_dotenv()
