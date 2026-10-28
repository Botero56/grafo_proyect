"""Pone recordatorio_menu_hora = "09:30" a las Configurations que hoy reciben
el recordatorio de "aún no tengo el menú" (negocio activo con admins), para
que al desplegar la hora por negocio nada cambie. A las que ya tienen el
campo no las toca.

Por defecto NO escribe: muestra qué cambiaría. Con --apply escribe solo
ese campo.

    PROJECT_ID=j2ys-tech-chat-automation DATABASE=j2ys-tech-core \
        python -m ops.set_reminder_hour            # dry-run
    ... python -m ops.set_reminder_hour --apply   # escribe

Autentica con ADC. Idempotente.
"""

import argparse

from google.cloud import firestore

from app.config import firestore_settings

# La hora fija de antes: job `30 9 * * *` (RUNBOOK §6).
PREVIOUS_HOUR = "09:30"


def planned_changes(configurations, active_business_ids: set[str]) -> list[tuple[str, dict]]:
    """[(doc_id, {"recordatorio_menu_hora": "09:30"})] de las que cambian."""
    return [
        (doc_id, {"recordatorio_menu_hora": PREVIOUS_HOUR})
        for doc_id, data in configurations
        if "recordatorio_menu_hora" not in data
        and data.get("business_id") in active_business_ids
        and data.get("admin_phone_numbers")
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="escribe los cambios (sin esto, solo los muestra)")
    args = parser.parse_args()

    client = firestore.Client(project=firestore_settings.PROJECT_ID, database=firestore_settings.DATABASE)
    active = {b.id for b in client.collection("businesses").where("status", "==", "active").stream()}
    collection = client.collection("configurations")
    changes = planned_changes(((d.id, d.to_dict() or {}) for d in collection.stream()), active)
    for doc_id, update in changes:
        print(f"{doc_id}: {update}")
        if args.apply:
            collection.document(doc_id).update(update)
    print(f"{len(changes)} documento(s) {'actualizados' if args.apply else 'por actualizar (dry-run)'}")


if __name__ == "__main__":
    main()
