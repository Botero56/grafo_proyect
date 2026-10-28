#!/usr/bin/env bash
# Pasa a Secret Manager las variables sensibles que hoy están en texto plano
# en Cloud Run (APP_SECRET, VERIFY_TOKEN, API_KEY, TEMP_TOKEN_WPP) y las
# monta como secret refs. Los valores se copian de la revisión actual SIN
# mostrarlos ni escribirlos en disco ni en el historial de la terminal.
#
#   PROJECT=j2ys-tech-chat-automation ./ops/secrets_to_refs.sh          # muestra el plan
#   PROJECT=j2ys-tech-chat-automation APPLY=1 ./ops/secrets_to_refs.sh  # lo ejecuta
#
# Correr ANTES de ops/iam.sh no importa: los nombres de secreto ya encajan
# con las condiciones de acceso por prefijo de ese script.
#
# También quita variables que el código ya no lee (PRINT_URL, META_*,
# SERVICE_ROLE) y desmonta KAPSO_API_KEY del webhook, que no envía mensajes
# (con la cuenta j2ys-webhook de ops/iam.sh ya no podría leerlo). PRINT_URL se convierte en LEGACY_PRINT_URL para el negocio
# que lo usa hoy, hasta que tenga su impresora configurada.
set -euo pipefail

PROJECT="${PROJECT:?Define PROJECT}"
REGION="${REGION:-us-central1}"
WORKER_SERVICE="${WORKER_SERVICE:-j2ys-lt-app}"
WEBHOOK_SERVICE="${WEBHOOK_SERVICE:-j2ys-lt-plain}"
# Único negocio real que imprime hoy por el PRINT_URL global (Edy Gourmet).
LEGACY_PRINT_BUSINESS_ID="${LEGACY_PRINT_BUSINESS_ID:-k5c6JI3uMiUbio5GNvFM}"

run() { if [[ -n "${APPLY:-}" ]]; then "$@"; else echo "[plan] ${*:0:6} ..."; fi; }

env_value() { # servicio variable -> valor actual en texto plano (vacío si no está)
  gcloud run services describe "$1" --region "$REGION" --project "$PROJECT" --format=json \
    | python3 -c "import json,sys; e=json.load(sys.stdin)['spec']['template']['spec']['containers'][0].get('env',[]); print(next((x.get('value','') for x in e if x['name']=='$2' and 'value' in x),''), end='')"
}

to_secret() { # servicio variable nombre-del-secreto
  local value
  value=$(env_value "$1" "$2")
  if [[ -z "$value" ]]; then
    echo "  $2 no está en texto plano en $1 (¿ya migrada?)"
    return
  fi
  if ! gcloud secrets describe "$3" --project "$PROJECT" >/dev/null 2>&1; then
    run gcloud secrets create "$3" --project "$PROJECT" --replication-policy=automatic
  fi
  # El valor viaja por stdin, nunca como argumento.
  if [[ -n "${APPLY:-}" ]]; then
    printf '%s' "$value" | gcloud secrets versions add "$3" --project "$PROJECT" --data-file=- >/dev/null
  fi
  echo "  $2 -> secreto $3"
}

echo "Webhook ($WEBHOOK_SERVICE)"
to_secret "$WEBHOOK_SERVICE" APP_SECRET app-secret
to_secret "$WEBHOOK_SERVICE" VERIFY_TOKEN verify-token
echo "Worker ($WORKER_SERVICE)"
to_secret "$WORKER_SERVICE" API_KEY admin-api-key
to_secret "$WORKER_SERVICE" TEMP_TOKEN_WPP meta-system-token

print_url=$(env_value "$WORKER_SERVICE" PRINT_URL)

# Se quita la variable en texto plano y se monta el secreto en la misma
# revisión. Si gcloud se queja del cambio de tipo, corre primero solo el
# --remove-env-vars y enseguida el --update-secrets.
run gcloud run services update "$WEBHOOK_SERVICE" --region "$REGION" --project "$PROJECT" \
  --remove-env-vars APP_SECRET,VERIFY_TOKEN,TEMP_TOKEN_WPP,API_KEY,PRINT_URL,META_APP_ID,META_CONFIG_ID,META_ES_COEXISTENCE_FEATURE_TYPE,SERVICE_ROLE \
  --update-secrets APP_SECRET=app-secret:latest,VERIFY_TOKEN=verify-token:latest   --remove-secrets KAPSO_API_KEY

run gcloud run services update "$WORKER_SERVICE" --region "$REGION" --project "$PROJECT" \
  --remove-env-vars APP_SECRET,VERIFY_TOKEN,TEMP_TOKEN_WPP,API_KEY,PRINT_URL,SERVICE_ROLE \
  --update-secrets API_KEY=admin-api-key:latest,TEMP_TOKEN_WPP=meta-system-token:latest \
  --update-env-vars "LEGACY_PRINT_URL=$print_url,LEGACY_PRINT_BUSINESS_ID=$LEGACY_PRINT_BUSINESS_ID"

[[ -z "${APPLY:-}" ]] && echo "Plan mostrado. Ejecuta con APPLY=1."
