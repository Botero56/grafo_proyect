#!/usr/bin/env bash
# Respaldo de Firestore (A8). Hoy la base j2ys-tech-core tiene PITR y la
# protección contra borrado DESACTIVADOS y ningún backup programado.
#
#   PROJECT=j2ys-tech-chat-automation BUCKET=gs://j2ys-firestore-exports ./ops/firestore_backups.sh
#
#   1. PITR: lectura de cualquier versión de los últimos 7 días (minuto a minuto).
#   2. Protección contra borrado de la base.
#   3. Backup programado diario (retención 14 días) y semanal (98 días).
#   4. Export diario a GCS (copia fuera de Firestore, restaurable a otra base),
#      por Cloud Scheduler. Opcional: solo si defines BUCKET.
#
# Restaurar: docs/RUNBOOK.md, "Restaurar Firestore".
set -euo pipefail

PROJECT="${PROJECT:?Define PROJECT}"
DATABASE="${DATABASE:-j2ys-tech-core}"
REGION="${REGION:-us-central1}"

gcloud firestore databases update --project "$PROJECT" --database="$DATABASE" \
  --enable-pitr --delete-protection

if [[ -z "$(gcloud firestore backups schedules list --project "$PROJECT" --database="$DATABASE" --format='value(name)')" ]]; then
  gcloud firestore backups schedules create --project "$PROJECT" --database="$DATABASE" --recurrence=daily --retention=14d
  gcloud firestore backups schedules create --project "$PROJECT" --database="$DATABASE" --recurrence=weekly --day-of-week=SUN --retention=98d
fi

if [[ -n "${BUCKET:-}" ]]; then
  gcloud storage buckets describe "$BUCKET" --project "$PROJECT" >/dev/null 2>&1 \
    || gcloud storage buckets create "$BUCKET" --project "$PROJECT" --location "$REGION" --uniform-bucket-level-access
  # Borra exports de más de 30 días.
  printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}' >/tmp/j2ys-lifecycle.json
  gcloud storage buckets update "$BUCKET" --lifecycle-file=/tmp/j2ys-lifecycle.json
  EXPORTER="j2ys-firestore-export@$PROJECT.iam.gserviceaccount.com"
  gcloud iam service-accounts describe "$EXPORTER" --project "$PROJECT" >/dev/null 2>&1 \
    || gcloud iam service-accounts create j2ys-firestore-export --project "$PROJECT"
  gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$EXPORTER" \
    --role=roles/datastore.importExportAdmin --condition=None --quiet >/dev/null
  gcloud storage buckets add-iam-policy-binding "$BUCKET" --member="serviceAccount:$EXPORTER" \
    --role=roles/storage.objectAdmin --quiet >/dev/null
  gcloud scheduler jobs describe j2ys-firestore-export --location "$REGION" --project "$PROJECT" >/dev/null 2>&1 \
    || gcloud scheduler jobs create http j2ys-firestore-export --location "$REGION" --project "$PROJECT" \
      --schedule="30 3 * * *" --time-zone="America/Bogota" --http-method=POST \
      --uri="https://firestore.googleapis.com/v1/projects/$PROJECT/databases/$DATABASE:exportDocuments" \
      --message-body="{\"outputUriPrefix\":\"$BUCKET/export\"}" \
      --oauth-service-account-email="$EXPORTER"
fi

gcloud firestore databases describe --project "$PROJECT" --database="$DATABASE" \
  --format="table(pointInTimeRecoveryEnablement,deleteProtectionState,versionRetentionPeriod)"
