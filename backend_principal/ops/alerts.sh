#!/usr/bin/env bash
# Alertas de j2ys como código (Cloud Monitoring, alertas basadas en logs).
#
# Uso (Cloud Shell o gcloud local con permisos de Monitoring Editor):
#
#   PROJECT=j2ys-tech-chat-automation ALERT_EMAIL=ops@ejemplo.com ./ops/alerts.sh
#   # o con un canal ya creado (correo, SMS, Slack...):
#   PROJECT=... ALERT_CHANNELS=projects/<p>/notificationChannels/123 ./ops/alerts.sh
#
# Variables:
#   PROJECT          proyecto de GCP (obligatoria)
#   ALERT_EMAIL      crea (o reutiliza) un canal de correo
#   ALERT_SMS        crea (o reutiliza) un canal SMS, formato +573001234567
#   ALERT_CHANNELS   canales existentes, separados por coma
#   WORKER_SERVICE   servicio de Cloud Run del worker (default j2ys-lt-app)
#
# Idempotente: una política con el mismo nombre no se vuelve a crear (para
# cambiarla, bórrala en la consola y corre de nuevo).
#
# Alertas:
#   1. Turno de IA con outcome != "ok"
#   2. 5xx / 401 / 403 en /tasks/* y en los jobs de Scheduler del worker
#   3. Mensajes sin respuesta en un negocio abierto (ops_event=unanswered_messages,
#      lo emite el job check-unanswered; ver app/services/messaging/unanswered_job.py)
#   4. Aviso al admin no entregado (ops_event=admin_notice_undelivered)
set -euo pipefail

PROJECT="${PROJECT:?Define PROJECT}"
WORKER_SERVICE="${WORKER_SERVICE:-j2ys-lt-app}"

channels=()
if [[ -n "${ALERT_CHANNELS:-}" ]]; then
  IFS=',' read -r -a channels <<<"$ALERT_CHANNELS"
fi

channel_for() { # tipo etiqueta valor nombre
  local existing
  existing=$(gcloud beta monitoring channels list --project "$PROJECT" \
    --filter="type=\"$1\" AND labels.$2=\"$3\"" --format='value(name)' | head -n1)
  if [[ -n "$existing" ]]; then
    echo "$existing"
  else
    gcloud beta monitoring channels create --project "$PROJECT" --display-name="$4" \
      --type="$1" --channel-labels="$2=$3" --format='value(name)'
  fi
}

[[ -n "${ALERT_EMAIL:-}" ]] && channels+=("$(channel_for email email_address "$ALERT_EMAIL" "j2ys alertas (correo)")")
[[ -n "${ALERT_SMS:-}" ]] && channels+=("$(channel_for sms number "$ALERT_SMS" "j2ys alertas (SMS)")")
if [[ ${#channels[@]} -eq 0 ]]; then
  echo "Define ALERT_EMAIL, ALERT_SMS o ALERT_CHANNELS" >&2
  exit 1
fi
channels_json=$(printf '"%s",' "${channels[@]}")
channels_json="[${channels_json%,}]"

RUN="resource.type=\\\"cloud_run_revision\\\" AND resource.labels.service_name=\\\"$WORKER_SERVICE\\\""

policy() { # nombre filtro documentación
  local name="$1" filter="$2" doc="$3" file
  if [[ -n "$(gcloud alpha monitoring policies list --project "$PROJECT" \
      --filter="displayName=\"$name\"" --format='value(name)')" ]]; then
    echo "Ya existe: $name"
    return
  fi
  file=$(mktemp)
  cat >"$file" <<JSON
{
  "displayName": "$name",
  "combiner": "OR",
  "severity": "ERROR",
  "conditions": [{"displayName": "$name", "conditionMatchedLog": {"filter": "$filter"}}],
  "alertStrategy": {"notificationRateLimit": {"period": "300s"}, "autoClose": "1800s"},
  "notificationChannels": $channels_json,
  "documentation": {"mimeType": "text/markdown", "content": "$doc Ver docs/RUNBOOK.md."}
}
JSON
  gcloud alpha monitoring policies create --project "$PROJECT" --policy-from-file="$file"
  rm -f "$file"
}

policy "j2ys: turno de IA fallido" \
  "$RUN AND jsonPayload.message=\\\"Turno de IA\\\" AND jsonPayload.outcome!=\\\"ok\\\"" \
  "Un turno de IA terminó con outcome distinto de ok (jsonPayload.outcome y business_id en el log)."

policy "j2ys: tareas con error" \
  "$RUN AND (httpRequest.requestUrl:\\\"/tasks/\\\" OR httpRequest.requestUrl:\\\"/api/v1/admin/jobs/\\\") AND (httpRequest.status>=500 OR httpRequest.status=401 OR httpRequest.status=403)" \
  "Cloud Tasks o un job de Scheduler recibe 5xx o 401/403 (tareas: OIDC, revisar WORKER_SERVICE_ACCOUNT_EMAIL / WORKER_URL; jobs: API key admin o run.invoker)."

policy "j2ys: mensajes sin respuesta" \
  "$RUN AND jsonPayload.ops_event=\\\"unanswered_messages\\\"" \
  "Un cliente de un negocio abierto lleva más de 3 min sin respuesta (business_id y conversation_id en el log)."

policy "j2ys: aviso al admin no entregado" \
  "$RUN AND jsonPayload.ops_event=\\\"admin_notice_undelivered\\\"" \
  "Un aviso al admin (comanda, cotización, handoff o recordatorio) no salió: falta plantilla (notice_kind) o WhatsApp lo rechazó."

echo "Listo. Canales: $channels_json"
