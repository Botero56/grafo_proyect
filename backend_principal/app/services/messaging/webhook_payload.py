"""Utilidades PURAS del payload del webhook de WhatsApp: separar una entrega
en eventos sueltos y leer sus identificadores.

Sin Firestore, sin google-adk, sin fireo: eso es a propósito. Un ingress
liviano (webhook -> Cloud Tasks) tiene que poder importar esto sin arrastrar
el peso del worker. Ver docs de la migración a Cloud Tasks.

Meta puede agrupar varias `entry`, varios `changes` y varios `messages` (o
`message_echoes`) en UNA sola entrega HTTP -- pasa con reintentos y cuando
se acumula backlog. `WhatsAppWebhookEventRouter.route` y
`WhatsAppWebhookService.process_incoming_payload` leen `entry[0]`,
`changes[0]` y `messages[0]`, así que sin separar la entrega TODO lo que no
sea el primer mensaje se descarta en silencio.
"""

from typing import Optional

# Listas que representan varios eventos independientes dentro de un mismo
# `value`. Los `statuses` NO están acá: _handle_status_update ya los recorre
# todos internamente, separarlos duplicaría ese trabajo.
_SPLITTABLE_KEYS = ("messages", "message_echoes")


def split_events(body: dict) -> list[dict]:
    """Descompone una entrega en payloads de UN solo evento.

    Cada payload conserva la forma original (`entry` -> `changes` -> `value`,
    con su `field`), así que cualquier consumidor que hoy lee `[0]` sigue
    funcionando sin cambios y ahora ve todos los eventos, no solo el primero.

    Un `value` sin `messages` ni `message_echoes` (statuses, history,
    smb_app_state_sync) no se separa: se devuelve tal cual, una vez. Un body
    que no tiene la forma esperada también se devuelve tal cual, para que el
    consumidor decida qué hacer con él (hoy: registrar `parse_error`).
    """
    entries = body.get("entry")
    if not isinstance(entries, list):
        return [body]

    out: list[dict] = []
    # Cambios de historial/contactos que vienen MEZCLADOS con mensajes en la
    # misma entrega: sin esto se perdían, porque solo se devolvía lo
    # separable.
    sync_changes: list[dict] = []
    splittable_found = False

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue

            key = next((k for k in _SPLITTABLE_KEYS if isinstance(value.get(k), list)), None)
            if key is None:
                if change.get("field") in SYNC_FIELDS:
                    sync_changes.append({**body, "entry": [{**entry, "changes": [change]}]})
                continue

            splittable_found = True
            # `contacts` viene a nivel de `value`, en paralelo a `messages`:
            # se empareja por wa_id para que cada payload separado conserve
            # el contacto de SU mensaje (de ahí sale el nombre del cliente).
            contacts = [c for c in (value.get("contacts") or []) if isinstance(c, dict)]
            by_wa_id = {c.get("wa_id"): c for c in contacts}

            for item in value[key]:
                if not isinstance(item, dict):
                    continue
                single_value = {**value, key: [item]}
                if key == "messages":
                    contact = by_wa_id.get(item.get("from"))
                    single_value["contacts"] = [contact] if contact else contacts[:1]
                out.append(
                    {
                        **body,
                        "entry": [{**entry, "changes": [{**change, "value": single_value}]}],
                    }
                )

    # Sin nada separable (status/history/app_state_sync) la entrega viaja
    # entera: la separación no debe hacer desaparecer eventos que hoy sí se
    # procesan.
    return out + sync_changes if splittable_found else [body]


def event_id(payload: dict) -> Optional[str]:
    """`wamid` de un payload YA separado (mensaje de cliente o echo).

    Es el identificador que usa la idempotencia (`WebhookEvents`) y el que
    sirve como nombre determinista de tarea al encolar en Cloud Tasks. None
    para los eventos que no traen wamid (statuses, history), que se
    deduplican con un hash de su contenido.
    """
    try:
        value = payload["entry"][0]["changes"][0]["value"]
    except (KeyError, IndexError, TypeError):
        return None

    for key in _SPLITTABLE_KEYS:
        items = value.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0].get("id")
    return None


# Webhooks de coexistencia que llegan en ráfaga al conectar un número y no
# traen un wamid propio: van a una cola aparte (ver
# CloudTasksSettings.CLOUD_TASKS_HISTORY_QUEUE).
SYNC_FIELDS = ("history", "smb_app_state_sync")


def event_field(payload: dict) -> Optional[str]:
    """`changes[0].field` de un payload (ej. "messages", "history")."""
    try:
        return payload["entry"][0]["changes"][0].get("field")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def content_event_id(payload: dict) -> str:
    """Id determinista para eventos sin wamid: `field:` + hash del `value`.

    Sirve como nombre de tarea en Cloud Tasks, así que una reentrega de
    Meta con el mismo contenido no se encola dos veces. El worker además
    deduplica en WebhookEvents con el mismo criterio."""
    import hashlib
    import json

    try:
        value = payload["entry"][0]["changes"][0].get("value")
    except (KeyError, IndexError, TypeError, AttributeError):
        value = payload
    digest = hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"{event_field(payload) or 'unknown'}:{digest}"


def split_history(payload: dict) -> list[dict]:
    """Separa un webhook `history` (YA separado por split_events) en un
    payload por hilo (= por cliente).

    Un bloque de historial puede traer muchos chats; separado, cada tarea
    es chica (Cloud Tasks admite hasta 1 MB por tarea) y Firestore recibe
    las escrituras repartidas en el tiempo según la cola. Los bloques con
    `errors` (el dueño no compartió el historial) viajan tal cual. Cualquier
    otra cosa se devuelve sin tocar."""
    if event_field(payload) != "history":
        return [payload]
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]
        value = change["value"]
    except (KeyError, IndexError, TypeError):
        return [payload]

    out: list[dict] = []
    for chunk in value.get("history") or []:
        if not isinstance(chunk, dict):
            continue
        threads = chunk.get("threads")
        if not isinstance(threads, list) or not threads:
            pieces = [chunk]
        else:
            pieces = [{**chunk, "threads": [t]} for t in threads if isinstance(t, dict)]
        for piece in pieces:
            single_value = {**value, "history": [piece]}
            out.append(
                {**payload, "entry": [{**entry, "changes": [{**change, "value": single_value}]}]}
            )
    return out or [payload]


def phone_number_ids(body: dict) -> set[str]:
    """Todos los `metadata.phone_number_id` de una entrega (sin separar).
    El webhook por tenant exige que sean todos del número de ese tenant."""
    ids = set()
    for entry in body.get("entry") or []:
        for change in (entry.get("changes") or []) if isinstance(entry, dict) else []:
            value = change.get("value") if isinstance(change, dict) else None
            metadata = value.get("metadata") if isinstance(value, dict) else None
            if isinstance(metadata, dict) and metadata.get("phone_number_id"):
                ids.add(str(metadata["phone_number_id"]))
    return ids


def event_timestamp(payload: dict) -> Optional[int]:
    """`timestamp` (epoch en segundos, string en el JSON de Meta) del mensaje
    o echo de un payload YA separado; None si no trae."""
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        for key in _SPLITTABLE_KEYS:
            items = value.get(key)
            if isinstance(items, list) and items:
                return int(items[0]["timestamp"])
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        pass
    return None
