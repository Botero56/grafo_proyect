"""Normaliza manager_phone y admin_phone_numbers de todas las Configurations
(solo dígitos, con indicativo; "3001234567" -> "573001234567").

Por defecto NO escribe: muestra qué cambiaría. Con --apply actualiza solo
esos dos campos de los documentos que cambian.

    PROJECT_ID=j2ys-tech-chat-automation DATABASE=j2ys-tech-core \
        python -m ops.migrate_phones            # dry-run
    ... python -m ops.migrate_phones --apply   # escribe

Autentica con ADC (gcloud auth application-default login). Idempotente.
"""

import argparse

from google.cloud import firestore

from app.config import firestore_settings
from app.services.phones import normalize_phone


def planned_changes(docs) -> list[tuple[str, dict]]:
    """[(doc_id, {campo: valor_nuevo})] de los documentos que cambian."""
    changes = []
    for doc_id, data in docs:
        update = {}
        manager = data.get("manager_phone")
        if manager and normalize_phone(manager) != manager:
            update["manager_phone"] = normalize_phone(manager)
        admins = data.get("admin_phone_numbers") or []
        normalized = list(dict.fromkeys(normalize_phone(p) for p in admins if normalize_phone(p)))
        if normalized != admins:
            update["admin_phone_numbers"] = normalized
        if update:
            changes.append((doc_id, update))
    return changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="escribe los cambios (sin esto, solo los muestra)")
    args = parser.parse_args()

    client = firestore.Client(project=firestore_settings.PROJECT_ID, database=firestore_settings.DATABASE)
    collection = client.collection("configurations")
    changes = planned_changes((d.id, d.to_dict() or {}) for d in collection.stream())
    for doc_id, update in changes:
        print(f"{doc_id}: {update}")
        if args.apply:
            collection.document(doc_id).update(update)
    print(f"{len(changes)} documento(s) {'actualizados' if args.apply else 'por actualizar (dry-run)'}")


if __name__ == "__main__":
    main()
