"""Dirección escrita por el cliente -> zona de domicilio del catálogo.

Los clientes escriben la dirección como les sale: "cerro asul torre 3",
"Laureles de terranova apto 202", "en la ceiba". Se normaliza (minúsculas,
sin tildes ni signos) y se busca el nombre o un alias de cada zona como
frase completa, o con un error de tipeo menor (SequenceMatcher). El
catálogo vive en Configurations.delivery_zones del negocio.

Solo se devuelve una zona cuando hay UNA coincidencia: si hay dos (o
ninguna) la tarifa no se adivina -- la IA le pregunta al cliente y, si no
es de ninguna zona, se cotiza a mano (ver quotes.py).
"""

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

from app.firestore_db.firebase_schemas import Configurations
from app.services.firestore_services.tenant_scoped_service import TenantScopedQuery

# Un error de tipeo en una palabra ("asul" por "azul") pasa; "cerro claro"
# contra "cerro azul" no.
_FUZZY_THRESHOLD = 0.85


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _mentions(address_words: list[str], candidate: str) -> bool:
    words = normalize(candidate).split()
    if not words:
        return False
    n = len(words)
    phrase = " ".join(words)
    windows = (" ".join(address_words[i : i + n]) for i in range(len(address_words) - n + 1))
    return any(w == phrase or SequenceMatcher(None, w, phrase).ratio() >= _FUZZY_THRESHOLD for w in windows)


def matching_zones(address: str, zones: list[dict]) -> list[dict]:
    """Todas las zonas cuyo nombre o algún alias aparece en la dirección."""
    words = normalize(address).split()
    return [
        zone
        for zone in zones
        if any(_mentions(words, name) for name in [zone.get("nombre", ""), *(zone.get("alias") or [])])
    ]


# Para la lista de opciones basta con parecerse ("belem" ~ "belen").
_CANDIDATE_THRESHOLD = 0.75
# La lista de WhatsApp admite 10 filas: la última es "Ninguno de estos".
MAX_CHOICES = 9
# Id de cada fila de esa lista: "zona:{índice en zona_opciones}" o "zona:ninguna".
CHOICE_PREFIX = "zona:"
CHOICE_NONE = "ninguna"


def candidate_zones(barrio: str, zones: list[dict]) -> list[dict]:
    """Zonas que pueden ser el barrio del cliente cuando find_zone no dio
    una sola: su nombre o alias contiene lo que dijo ("Belén" -> "Belén
    Rincón", "Belén Las Playas"), está dentro de lo que dijo, o se le
    parece (tipeo). De la más a la menos parecida."""
    key = normalize(barrio)
    if len(key) < 3:
        return []
    scored = []
    for zone in zones:
        names = [n for n in (normalize(x) for x in [zone.get("nombre", ""), *(zone.get("alias") or [])]) if n]
        score = max(
            (1.0 if f" {key} " in f" {n} " or f" {n} " in f" {key} " else SequenceMatcher(None, key, n).ratio() for n in names),
            default=0.0,
        )
        if score >= _CANDIDATE_THRESHOLD:
            scored.append((score, zone))
    scored.sort(key=lambda s: -s[0])
    return [zone for _, zone in scored[:MAX_CHOICES]]


def zones_for(business_id: str) -> list[dict]:
    configuration = TenantScopedQuery.get_one_for_business(Configurations, business_id)
    return list(getattr(configuration, "delivery_zones", None) or []) if configuration else []


def find_zone(address: str, zones: list[dict]) -> Optional[dict]:
    """La zona de la dirección, o None si no queda clara.

    Con varias coincidencias gana la más específica: si el nombre (o alias)
    que coincidió de una contiene el de todas las demás, es esa ("Belén
    Rincón" le gana a "Belén"). Si no, None: la tarifa no se adivina."""
    words = normalize(address).split()
    hits = []
    for zone in zones:
        found = [normalize(n) for n in [zone.get("nombre", ""), *(zone.get("alias") or [])] if _mentions(words, n)]
        if found:
            hits.append((zone, max(found, key=len)))
    if len(hits) == 1:
        return hits[0][0]
    if not hits:
        return None
    best_zone, best = max(hits, key=lambda h: len(h[1]))
    others = [phrase for zone, phrase in hits if zone is not best_zone]
    if all(phrase != best and f" {phrase} " in f" {best} " for phrase in others):
        return best_zone
    return None
