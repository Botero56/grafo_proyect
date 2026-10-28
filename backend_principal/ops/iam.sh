#!/usr/bin/env bash
# Cuentas de servicio separadas con mínimo privilegio (A6). Reemplaza la
# cuenta única j2ys-lt-run (secretmanager.admin + datastore.user + aiplatform.user
# + run.invoker), con la que un fallo del webhook público alcanzaba todos los
# secretos y toda la base.
#
#   PROJECT=j2ys-tech-chat-automation ./ops/iam.sh
#
# Idempotente (add-iam-policy-binding no duplica). NO cambia los servicios:
# al final imprime los `gcloud run services update` para aplicarlos tú, uno
# por uno, después de ops/secrets_to_refs.sh. j2ys-lt-run se puede borrar
# cuando todo funcione con las nuevas.
#
#   j2ys-webhook        servicio público: lee businesses, encola tareas,
#                       lee kapso-webhook-secret--*, printer-token--* (heartbeat) y los secretos globales del webhook
#   j2ys-worker         worker privado: Firestore, Vertex, comprobantes,
#                       secretos por negocio de envío e impresión; crea/rota
#                       secretos de negocios desde la API admin
#   j2ys-tasks-invoker  identidad OIDC de Cloud Tasks y Cloud Scheduler:
#                       solo puede invocar al worker
set -euo pipefail

PROJECT="${PROJECT:?Define PROJECT}"
REGION="${REGION:-us-central1}"
WORKER_SERVICE="${WORKER_SERVICE:-j2ys-lt-app}"
WEBHOOK_SERVICE="${WEBHOOK_SERVICE:-j2ys-lt-plain}"
RECEIPTS_BUCKET="${RECEIPTS_BUCKET:-j2ys-tech-chat-automation-comprobantes}"
NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')

sa() { echo "$1@$PROJECT.iam.gserviceaccount.com"; }
WEBHOOK_SA=$(sa j2ys-webhook)
WORKER_SA=$(sa j2ys-worker)
INVOKER_SA=$(sa j2ys-tasks-invoker)

for name in j2ys-webhook j2ys-worker j2ys-tasks-invoker; do
  gcloud iam service-accounts describe "$(sa $name)" --project "$PROJECT" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$name" --project "$PROJECT" --display-name "$name"
done

project_role() { # miembro rol [condición]
  local args=(--member="serviceAccount:$1" --role="$2" --condition=None)
  [[ -n "${3:-}" ]] && args=(--member="serviceAccount:$1" --role="$2" --condition="$3")
  gcloud projects add-iam-policy-binding "$PROJECT" "${args[@]}" --quiet >/dev/null
  echo "  $1 -> $2${3:+ (condición)}"
}

# Acceso a secretos solo por prefijo (los nombres son <tipo>--<businessId>,
# ver app/services/secrets). Las condiciones usan el NÚMERO de proyecto.
prefix_condition() { # título prefijo...
  local title="$1" expr="" p
  shift
  for p in "$@"; do
    expr+="${expr:+ || }resource.name.startsWith(\"projects/$NUMBER/secrets/$p\")"
  done
  echo "expression=$expr,title=$title"
}

echo "Webhook ($WEBHOOK_SA)"
project_role "$WEBHOOK_SA" roles/datastore.viewer
project_role "$WEBHOOK_SA" roles/cloudtasks.enqueuer
project_role "$WEBHOOK_SA" roles/secretmanager.secretAccessor \
  "$(prefix_condition webhook-secrets kapso-webhook-secret app-secret verify-token printer-token)"

echo "Worker ($WORKER_SA)"
project_role "$WORKER_SA" roles/datastore.user
project_role "$WORKER_SA" roles/aiplatform.user
project_role "$WORKER_SA" roles/cloudtasks.enqueuer
project_role "$WORKER_SA" roles/secretmanager.secretAccessor \
  "$(prefix_condition worker-secrets kapso-api-key printer-token admin-api-key meta-system-token)"
# La API admin agrega versiones (rotación) a los secretos por negocio...
project_role "$WORKER_SA" roles/secretmanager.secretVersionAdder \
  "$(prefix_condition worker-secret-rotation kapso-api-key-- kapso-webhook-secret-- printer-token--)"
# ...y los crea la primera vez. Crear no da lectura de ningún secreto.
gcloud iam roles describe j2ysSecretCreator --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud iam roles create j2ysSecretCreator --project "$PROJECT" \
    --title "j2ys: crear secretos" --permissions secretmanager.secrets.create --stage GA
project_role "$WORKER_SA" "projects/$PROJECT/roles/j2ysSecretCreator"
gcloud storage buckets add-iam-policy-binding "gs://$RECEIPTS_BUCKET" \
  --member="serviceAccount:$WORKER_SA" --role=roles/storage.objectAdmin --quiet >/dev/null
echo "  $WORKER_SA -> storage.objectAdmin en gs://$RECEIPTS_BUCKET"

echo "Invocador ($INVOKER_SA)"
gcloud run services add-iam-policy-binding "$WORKER_SERVICE" --region "$REGION" --project "$PROJECT" \
  --member="serviceAccount:$INVOKER_SA" --role=roles/run.invoker --quiet >/dev/null
echo "  $INVOKER_SA -> run.invoker en $WORKER_SERVICE"
# Webhook y worker crean tareas que se firman como el invocador.
for member in "$WEBHOOK_SA" "$WORKER_SA"; do
  gcloud iam service-accounts add-iam-policy-binding "$INVOKER_SA" --project "$PROJECT" \
    --member="serviceAccount:$member" --role=roles/iam.serviceAccountUser --quiet >/dev/null
  echo "  $member -> actAs $INVOKER_SA"
done

OLD_SA=$(sa j2ys-lt-run)
cat <<EOF

Listo. Aplica en este orden (cada comando crea una revisión nueva). La lista
en WORKER_SERVICE_ACCOUNT_EMAIL evita cortes: se firma con la primera
cuenta y el worker acepta las dos mientras hay tareas viejas en cola.

  # 1. Worker: nueva identidad; firma como el invocador y acepta ambas.
  gcloud run services update $WORKER_SERVICE --region $REGION --project $PROJECT \
    --service-account $WORKER_SA \
    --update-env-vars "^;^WORKER_SERVICE_ACCOUNT_EMAIL=$INVOKER_SA,$OLD_SA"

  # 2. Webhook: nueva identidad; sus tareas se firman como el invocador.
  gcloud run services update $WEBHOOK_SERVICE --region $REGION --project $PROJECT \
    --service-account $WEBHOOK_SA \
    --update-env-vars WORKER_SERVICE_ACCOUNT_EMAIL=$INVOKER_SA

  # 3. Prueba un mensaje real. Con la cola vacía
  #    (gcloud tasks list --queue whatsapp-messages --location $REGION), deja solo el invocador:
  gcloud run services update $WORKER_SERVICE --region $REGION --project $PROJECT \
    --update-env-vars WORKER_SERVICE_ACCOUNT_EMAIL=$INVOKER_SA

  # 4. Retira la cuenta vieja:
  gcloud run services remove-iam-policy-binding $WORKER_SERVICE --region $REGION --project $PROJECT \
    --member=serviceAccount:$OLD_SA --role=roles/run.invoker
  gcloud projects remove-iam-policy-binding $PROJECT --member=serviceAccount:$OLD_SA --role=roles/secretmanager.admin
EOF
