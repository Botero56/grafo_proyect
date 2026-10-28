# RUNBOOK — j2ys (bot de WhatsApp para restaurantes)

Operación de producción después de la Fase 1. Todo lo que cambia GCP, Kapso o
Meta lo ejecuta una persona; el código nunca lo hace solo.

- Proyecto: `j2ys-tech-chat-automation` · región `us-central1` · Firestore `j2ys-tech-core`
- Webhook público: `j2ys-lt-plain` · Worker privado: `j2ys-lt-app` (también API admin)
- Único negocio real: **Edy Gourmet** `k5c6JI3uMiUbio5GNvFM` (número `1295952196934414`,
  hoy envía por **Meta directo** con `TEMP_TOKEN_WPP`). El resto son negocios de prueba.

---

## 1. Pasos manuales, en orden

Cada paso dice dónde se ejecuta. Lo marcado con ⚠️ cambia producción.

### 1.0 Antes del primer despliegue de la Fase 1

| # | Dónde | Qué |
|---|---|---|
| 1 | GCP ⚠️ | Mantener la impresión de Edy Gourmet durante la transición: agregar `LEGACY_PRINT_URL` (= el `PRINT_URL` actual) sin quitar `PRINT_URL`; el código viejo la ignora. Comando abajo |
| 2 | GCP ⚠️ | Habilitar Cloud Scheduler. **Hoy no está habilitado: los jobs de recordatorio y de reactivación nunca han corrido.**<br>`gcloud services enable cloudscheduler.googleapis.com` |
| 3 | GCP ⚠️ | Backups de Firestore (PITR, protección contra borrado, backups diario y semanal, export opcional):<br>`PROJECT=j2ys-tech-chat-automation BUCKET=gs://j2ys-firestore-exports ./ops/firestore_backups.sh` |

Comando del paso 1:
```bash
URL=$(gcloud run services describe j2ys-lt-app --region us-central1 --format=json   | python3 -c "import json,sys;e=json.load(sys.stdin)['spec']['template']['spec']['containers'][0]['env'];print(next(x['value'] for x in e if x['name']=='PRINT_URL'))")
gcloud run services update j2ys-lt-app --region us-central1   --update-env-vars LEGACY_PRINT_URL=$URL,LEGACY_PRINT_BUSINESS_ID=k5c6JI3uMiUbio5GNvFM
```

### 1.1 Desplegar el código (§3)

| # | Dónde | Qué |
|---|---|---|
| 4 | local | `./ops/deploy.sh`: tests en Cloud Build y las dos revisiones **sin tráfico** con la etiqueta `c-<sha>` |
| 5 | local | Prueba de humo (§3.2) contra las URLs etiquetadas |
| 6 | GCP ⚠️ | `update-traffic --to-latest`, primero el worker y después el webhook (§3.3) |

### 1.2 Secretos, identidades y alertas (después del paso 6)

| # | Dónde | Qué |
|---|---|---|
| 7 | GCP ⚠️ | Variables sensibles a Secret Manager y limpieza de las variables muertas. Primero el plan, después `APPLY=1`:<br>`PROJECT=j2ys-tech-chat-automation ./ops/secrets_to_refs.sh` |
| 8 | GCP ⚠️ | Cuentas de servicio separadas: `PROJECT=j2ys-tech-chat-automation ./ops/iam.sh`, y luego los 4 comandos que imprime, **en orden** |
| 9 | GCP ⚠️ | Alertas: `PROJECT=j2ys-tech-chat-automation ALERT_EMAIL=<correo> ./ops/alerts.sh` (con `ALERT_SMS=+57...` si se quiere SMS) |
| 10 | GCP ⚠️ | Jobs de Cloud Scheduler (§6) |
| 11 | local | Teléfonos de admin: `python -m ops.migrate_phones` (dry-run). Revisar la salida y luego `--apply` |
| 12 | — | Hecho (2026-10-20): la colección `whatsapp_credentials` ya no existe; se retiró `ops/delete_whatsapp_credentials.py` |

### 1.3 Por negocio (§8, checklist)

| # | Dónde | Qué |
|---|---|---|
| 13 | Meta/Kapso ⚠️ | Crear y enviar a aprobación las 4 plantillas UTILITY (§5) en la WABA del negocio |
| 14 | API admin | Cuando se aprueben: `PATCH /admin/configurations/{id}` con `admin_templates` |
| 15 | API admin | Horario: `PATCH /admin/configurations/{id}` con `opening_hours` |
| 16 | Raspberry + API admin | Impresora: generar el token, configurarlo en la Raspberry (§4) y en `PUT /admin/configurations/{id}/printer`. Probar una comanda. Después quitar `LEGACY_PRINT_URL`/`LEGACY_PRINT_BUSINESS_ID` |
| 17 | API admin | Apagar los negocios de prueba duplicados (§7.1) |

---

## 2. Arquitectura en una pantalla

```
Kapso/Meta ──firma──▶ j2ys-lt-plain (webhook público)
                         │ verifica la firma del negocio, separa eventos
                         ▼ Cloud Tasks (OIDC j2ys-tasks-invoker)
                      j2ys-lt-app (worker privado, max 1 instancia)
                         ├─ /tasks/process-event   ingesta (ids deterministas, create())
                         ├─ /tasks/flush-conversation  claim atómico (compare-and-set) → IA → respuesta
                         └─ /api/v1/admin/*        API admin (X-API-Key) + jobs de Scheduler
```

- Concurrencia: cliente `{business_id}_{hash del teléfono}`, conversación `{customer_id}_c{n}` y
  mensaje `wa_<hash del wamid>`, todos creados con `create()`, que falla si el documento
  ya existe. El flush avanza la marca de agua con compare-and-set en una transacción.
  Los documentos viejos con id automático se siguen encontrando por consulta, sin migración.
- Kill switch: `businesses.status != "active"`. Los mensajes se guardan sin pasar por la IA.

---

## 3. Despliegue y rollback

### 3.1 Desplegar
```bash
./ops/deploy.sh          # exige árbol limpio; sube git archive HEAD; tests + build + deploy --no-traffic --tag c-<sha>
```

### 3.2 Prueba de humo (revisiones etiquetadas, 0 % de tráfico)
```bash
TAG=c-$(git rev-parse --short=8 HEAD)
W=https://$TAG---j2ys-lt-plain-ywi3275i5a-uc.a.run.app
K=https://$TAG---j2ys-lt-app-ywi3275i5a-uc.a.run.app
curl -s -o /dev/null -w "%{http_code}\n" $W/                                   # 200 (landing)
curl -s -o /dev/null -w "%{http_code}\n" -X POST $W/webhook/kapso/clave_falsa  # 404
# Worker privado: hace falta run.invoker para tu usuario y la API key admin.
curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
     -H "X-API-Key: $(gcloud secrets versions access latest --secret admin-api-key)" \
     $K/api/v1/admin/businesses/k5c6JI3uMiUbio5GNvFM/bot-status               # {"status":"active",...}
```
Antes del paso 7, la API key todavía no está en Secret Manager: usa el valor actual de `API_KEY`.

### 3.3 Pasar a producción
```bash
gcloud run services update-traffic j2ys-lt-app   --region us-central1 --to-latest
gcloud run services update-traffic j2ys-lt-plain --region us-central1 --to-latest
```
Se usa `--to-latest` y no `--to-tags`: así los `gcloud run services update` posteriores
(secretos, identidades) entran solos. Con el tráfico fijado a una revisión, la revisión
nueva quedaría con 0 %.

### 3.4 Rollback
```bash
gcloud run revisions list --service j2ys-lt-app --region us-central1   # elegir la anterior
gcloud run services update-traffic j2ys-lt-app   --region us-central1 --to-revisions <rev>=100
gcloud run services update-traffic j2ys-lt-plain --region us-central1 --to-revisions <rev>=100
```
Revisiones conocidas antes de la Fase 1: worker `j2ys-lt-app-00064-ccm` y webhook `j2ys-lt-plain-00036-2gp`.
⚠️ El código viejo exige `PRINT_URL` y, en el webhook, `API_KEY`. Si ya corriste el paso 7,
vuelve a ponerlas antes de un rollback a esas revisiones.

---

## 4. Impresora por negocio

Configuración (el token es de solo escritura y nunca vuelve en una respuesta):
```http
PUT /api/v1/admin/configurations/{business_id}/printer
X-API-Key: ...
{"enabled": true, "endpoint_url": "https://<túnel>/print", "token": "<≥16 caracteres>",
 "paper_width": "80mm", "copies": 1, "timeout_seconds": 15}
```
- El token queda en Secret Manager como `printer-token--{business_id}`. Para rotarlo, envía otro `token`.
- `endpoint_url` debe ser `https://` y no puede llevar usuario ni clave.
- Sin impresora, con la impresora caída o sin token legible, la comanda va al chat del
  admin. Fuera de 24 h usa la plantilla `comanda_respaldo`.

**Contrato que debe cumplir la Raspberry** (`POST endpoint_url`):
```
Authorization: Bearer <token del negocio>
X-Business-Id: <business_id>
{"text": "...", "payment_method"?: "...", "copies"?: n, "paper_width"?: "58mm"|"80mm"}
```
- Comparar el token en tiempo constante (`hmac.compare_digest`).
- Responder 401 si el token no coincide y 403 si `X-Business-Id` no es el suyo.
- Un endpoint compartido por varios negocios guarda un token por `business_id`. Cada
  comanda trae el nombre de su negocio en el encabezado.
- Responder 2xx solo cuando la comanda quedó impresa. Cualquier otra respuesta hace que
  la comanda vaya al chat del admin.

Versión y actualización de la Raspberry (botón "Actualizar Raspberry" del panel, en Comandas).
Usan la misma URL y el mismo token; el token nunca pasa por el navegador:
```http
GET  /api/v1/admin/configurations/{business_id}/printer/device   -> GET  <endpoint_url>/../version
POST /api/v1/admin/configurations/{business_id}/printer/update   -> POST <endpoint_url>/../update
```
409 sin URL o token · 502 Raspberry caída o con error (trae su detalle) · 504 si no contesta
(update espera 25 s, por el corte de 30 s del proxy del panel). Código de la Raspberry:
`gitlab.com/boterosamuel3/backend_rasperry`.

Heartbeat de la Raspberry (estado en el panel, sección Comandas). La Raspberry lo manda cada
60 s al **webhook público**, con el mismo token:
```http
POST https://<webhook>/devices/heartbeat        -> 202, encola /tasks/device-heartbeat
Authorization: Bearer <token del negocio>
X-Business-Id: <business_id>
{"device_id": "rpi-...", "measured_at": "...", "version": "1.2.0", "printer_usb": true, ...}
GET  /api/v1/admin/configurations/{business_id}/printer/devices   -> estado calculado al leer
```
- El webhook compara el token con `printer-token--{business_id}` (por eso su cuenta lee
  `printer-token--*`, ver `ops/iam.sh`) y no toca Firestore; el worker guarda el estado actual en
  `devices/{business_id}--{device_id}` (un documento por Raspberry, sin historial).
- Estado: `desconectado` sin heartbeat en 210 s (3 perdidos), `degradado` sin impresora USB o si el
  último print falló, `operativo` si no. Un 2xx de `/print` = enviado al USB, no papel impreso.
- La Raspberry (>= 1.2.0) apunta por defecto a `https://j2ys-lt-plain-ywi3275i5a-uc.a.run.app/devices/heartbeat`;
  `HEARTBEAT_URL` en `/etc/raspberry-backend.env` la cambia y `HEARTBEAT_URL=off` lo apaga.

Transición: mientras `LEGACY_PRINT_URL`/`LEGACY_PRINT_BUSINESS_ID` estén definidas, solo
ese negocio imprime en esa URL, sin token y con un warning en el log. Se quitan con:
`gcloud run services update j2ys-lt-app --region us-central1 --remove-env-vars LEGACY_PRINT_URL,LEGACY_PRINT_BUSINESS_ID`

---

## 5. Plantillas para avisos al admin (fuera de la ventana de 24 h)

Se crean en la WABA del número del negocio (en Kapso, o en Meta mientras Edy Gourmet
esté en Meta directo). Categoría **UTILITY**, idioma **es**, sin encabezado ni botones.
El aviso completo viaja en `{{1}}` en una sola línea, separado por ` · `, con un máximo
de 900 caracteres. Ninguna empieza ni termina con una variable.

| Tipo (`admin_templates.<tipo>`) | Nombre sugerido | Cuerpo |
|---|---|---|
| `comanda_respaldo` | `j2ys_comanda_respaldo` | `Hay un pedido para tu negocio que necesita tu atención: {{1}}. Responde a este mensaje para recibir aquí los siguientes avisos.` |
| `cotizacion_domicilio` | `j2ys_cotizacion_domicilio` | `Un cliente necesita que le cotices el domicilio: {{1}}. Responde a este mensaje con el valor para continuar con su pedido.` |
| `atencion_cliente` | `j2ys_atencion_cliente` | `Un cliente necesita que lo atienda una persona: {{1}}. Responde a este mensaje para ver los detalles y atenderlo.` |
| `recordatorio_menu` | `j2ys_recordatorio_admin` | `Hola, te escribe tu asistente de pedidos: {{1}}. Responde a este mensaje cuando lo tengas listo.` — `{{1}}` = `Configurations.recordatorio_menu_texto` (se edita en el panel, sección Recordatorio, sin nueva revisión de Meta). Enviada para kapso_test el 2026-10-25 (id 1467893111930000). |
| `cierre_diario` | `j2ys_cierre_diario` | `Cierre de domicilios de hoy: {{1}}. Responde a este mensaje para recibir aquí los siguientes avisos.` |

Ejemplos de `{{1}}` para la revisión de Meta:
- comanda: `🧾 Nuevo pedido confirmado · PEDIDO #A1B2C3 · Cliente: Ana · Dirección: Cra 1 #2-3 · 1x Menú del día · Total: $21.500 · Pago: EFECTIVO`
- cotización: `🔔 Ana necesita atención humana (cotizar_domicilio). Dirección fuera de las zonas: Vereda El Salado.`
- atención: `🚨 Cliente solicita atención · 👤 Ana · 📱 3001234567 · 📝 Reclamo por un pedido incompleto.`
- cierre: `📋 Cierre de domicilios 2026-10-16 · Comandas del día: 24 · Liquidadas: 22 · No liquidadas (2): #9 (efectivo), #17 · Efectivo sin liquidar: $21.000`

Cuando las aprueben:
```http
PATCH /api/v1/admin/configurations/{business_id}
{"admin_templates": {
  "comanda_respaldo":     {"name": "j2ys_comanda_respaldo",     "language": "es"},
  "cotizacion_domicilio": {"name": "j2ys_cotizacion_domicilio", "language": "es"},
  "atencion_cliente":     {"name": "j2ys_atencion_cliente",     "language": "es"},
  "recordatorio_menu":    {"name": "j2ys_recordatorio_admin",   "language": "es"},
  "cierre_diario":        {"name": "j2ys_cierre_diario",        "language": "es"}}}
```
Hasta entonces, cada aviso fuera de 24 h dispara la alerta "aviso al admin no entregado".
Es intencional.

---

## 6. Jobs de Cloud Scheduler

Todos llaman al worker con OIDC de `j2ys-tasks-invoker` y la API key admin. La key queda
visible en la configuración del job para quien tenga acceso a Scheduler; se rota con
`gcloud secrets versions add admin-api-key` y luego se actualizan los jobs.
```bash
WORKER=https://j2ys-lt-app-ywi3275i5a-uc.a.run.app
INVOKER=j2ys-tasks-invoker@j2ys-tech-chat-automation.iam.gserviceaccount.com
KEY=$(gcloud secrets versions access latest --secret admin-api-key)
job() { gcloud scheduler jobs create http "$1" --location us-central1 --schedule "$2" --time-zone America/Bogota \
  --http-method POST --uri "$WORKER/api/v1/admin/jobs/$3" --headers "X-API-Key=$KEY" \
  --oidc-service-account-email "$INVOKER" --oidc-token-audience "$WORKER"; }
job j2ys-check-unanswered     "*/5 * * * *"  check-unanswered
job j2ys-reactivate-bots      "*/10 * * * *" reactivate-bots
job j2ys-expire-daily-menus   "*/10 * * * *" expire-pending-daily-menus
job j2ys-remind-daily-menus   "*/10 * * * *" remind-missing-daily-menus   # avisa a cada negocio en su recordatorio_menu_hora
job j2ys-delivery-sweep       "*/10 * * * *" delivery-sweep               # presunto entregado + cierre diario de domicilios
```

---

## 7. Operación diaria

### 7.1 Kill switch (apagar o encender el bot de un negocio)
```http
PUT /api/v1/admin/businesses/{business_id}/bot-status
{"status": "inactive", "actor": "juan@j2ys.co", "reason": "motivo"}
```
- Con el bot apagado, los mensajes se guardan pero no pasan por la IA, y no se contestan
  al reactivar. Los jobs saltan el negocio.
- `actor` lo declara quien llama: con una sola API key, el servidor no puede comprobarlo (A7).
- Negocios de prueba que conviene apagar: los 8 duplicados "Edy Gourmet" (`UXvDNQGef6YWsR8VmT4G`,
  `Wo0OVAVCS3kNh9CWrDv0`, `XxiEhj2m0GCdmTdaNp15`, `fgQxCpqURlAQjOmGHjJY`, `klQKWbpajCG0BIKiClok`,
  `rHa2z1EMao6zD85oy0kf`, `sfwwu50BTSYisUAhim1H`, `uIbGI5hodCpaka7MiDsJ`). No tienen número
  propio, así que caen en el `PHONE_NUMBER_ID` global.

### 7.2 Horario
```http
PATCH /api/v1/admin/configurations/{business_id}
{"opening_hours": {"timezone": "America/Bogota", "enforce": true,
  "weekly": {"mon": [{"open": "11:00", "close": "15:00"}], "sat": [{"open": "18:00", "close": "02:00"}]},
  "exceptions": [{"date": "2026-12-25", "closed": true}]}}
```
- Un rango con `close <= open` termina al día siguiente.
- Con `enforce: false` el bot solo informa que el negocio está cerrado y no rechaza pedidos.
- "Hoy no abrimos" por el chat del admin cuenta como una excepción del día.

### 7.3 Qué hacer con cada alerta
| Alerta | Primer paso |
|---|---|
| turno de IA fallido | Logs: `jsonPayload.message="Turno de IA" AND jsonPayload.outcome!="ok"`. Ver el `outcome` (cuota de Vertex, timeout, sin texto) |
| tareas con error | 5xx: error del worker; Cloud Tasks reintenta. 401/403: OIDC. Revisar `WORKER_SERVICE_ACCOUNT_EMAIL` y `WORKER_URL` en los dos servicios |
| mensajes sin respuesta | Abrir la conversación (`conversation_id` en el log). Si son varias, revisar el worker y Vertex. Si hace falta, kill switch y atender a mano |
| aviso al admin no entregado | `notice_kind` indica qué plantilla falta. Configurarla (§5) o pedirle al admin que escriba al número para abrir la ventana de 24 h |

### 7.4 Restaurar Firestore
- Leer un estado anterior (PITR, hasta 7 días): consultas con `read_time`, o export con
  `gcloud firestore export gs://... --snapshot-time=<RFC3339>`.
- Restaurar un backup en una base **nueva** (nunca sobre la actual):
  `gcloud firestore backups list` y luego
  `gcloud firestore databases restore --source-backup=<backup> --destination-database=j2ys-restore`.
  Después se apunta `DATABASE` a la base restaurada o se copian los documentos.

---

## 8. Retirar el `/webhook` legado (B-4)

`POST /webhook` (firma global `APP_SECRET`/`KAPSO_WEBHOOK_SECRET`, sin tenant) sigue
existiendo porque **Edy Gourmet recibe por Meta directo**. Plan:
1. Conectar el número de Edy Gourmet en Kapso (setup link) y registrar en Kapso la URL de
   `GET /admin/businesses/k5c6JI3uMiUbio5GNvFM/webhook-url` con su secreto
   (`PUT .../kapso-credentials`).
2. Cambiar `businesses/k5c6JI3uMiUbio5GNvFM.whatsapp_provider` a `"kapso"` desde la consola
   de Firestore. No hay endpoint para esto.
3. Vigilar 7 días que no lleguen requests a `/webhook` (logs:
   `httpRequest.requestUrl:"/webhook" AND NOT httpRequest.requestUrl:"/webhook/kapso/"`).
4. Quitar la ruta `POST /webhook` y el `GET` de verificación, borrar los secretos globales
   `app-secret`, `verify-token` y `kapso-webhook-secret`, y quitar `TEMP_TOKEN_WPP`.

---

## 9. Cambios de API y de esquema para el front (Fase 1)

| Endpoint | Cambio |
|---|---|
| `GET/PUT /admin/configurations/{id}/printer` | **Nuevo.** Body: `{enabled, endpoint_url, token?, paper_width?, copies, timeout_seconds}`. Respuesta sin token: `token_configured` |
| `PATCH /admin/configurations/{id}` | **Nuevos** `opening_hours` y `admin_templates` (validados, 422 si son inválidos). `printer_settings` y `has_thermal_printer` **se ignoran**. `manager_phone` se guarda normalizado (`573001234567`); 400 si tiene menos de 8 dígitos |
| `GET /admin/configurations/{id}` | La respuesta incluye `opening_hours` y `admin_templates`. `has_thermal_printer` es una copia de `printer_settings.enabled` (obsoleto) |
| `PUT /admin/configurations/{id}/admin-phone-numbers` | Guarda normalizado y sin duplicados; 400 si un número es inválido |
| `GET/PUT /admin/businesses/{id}/bot-status` | **Nuevo.** `{status: "active"|"inactive", actor, reason?}`; respuesta `{status, changed_at, changed_by, reason}` |
| `POST /admin/jobs/check-unanswered` | **Nuevo** (solo para Scheduler) |
| `GET /onboarding`, `POST /api/whatsapp/onboarding` | **Eliminados** (onboarding propio retirado; los números se conectan por Kapso) |

### Panel interno (2026-10-20)

| Endpoint | Cambio |
|---|---|
| `GET /admin/businesses` | **Nuevo.** Todos los negocios, ordenados por nombre |
| `PATCH /admin/businesses/{id}` | **Nuevo.** Solo `name`, `legal_name`, `phone`, `whatsapp_phone_number`, `whatsapp_phone_number_id`, `whatsapp_provider`, `address`. 409 si el número o el id ya son de otro negocio |
| `POST /admin/businesses` | `whatsapp_phone_number_id` obligatorio (dígitos). 422 con una vertical sin agente (`dentist`). 409 con número o id repetidos. Teléfonos normalizados |
| `PUT /admin/configurations/{id}/couriers` | **Nuevo.** `{couriers: [{phone, name}]}`. 400 si un teléfono es admin. El GET de configuración trae `couriers` |
| `PATCH /admin/configurations/{id}` | **Nuevos** `cuentas_transferencia` (422 si se acepta transferencia sin cuenta), `recordatorio_menu_hora` (HH:MM; null = no recordar) y `daily_menu_expected_ready_time`. Zonas validadas (tarifa entera ≥0, alias de 4+ letras, nada repetido entre zonas). Topes de tamaño en `tono`, `faq` y `handoff_transfer_message`. **Ya no se aceptan ni se devuelven** `delivery_confirmation_*`, `manager_notification_delay_minutes` ni `has_thermal_printer` |
| `POST /admin/restaurant/fixed-menu-items` | `precio` entero ≥0 (obligatorio si `tipo_precio=fijo`), `tipo_precio` `fijo`\|`no_legible`, 404 si el negocio no existe. La respuesta trae `catalog_retailer_id` (con valor = lo mantiene la sincronización del catálogo) |

Al desplegar la hora por negocio: correr `python -m ops.set_reminder_hour --apply`
(pone `09:30` a los negocios que hoy reciben el recordatorio) y cambiar el job
`j2ys-remind-daily-menus` a `*/10 * * * *` (§6).

---

## 10. Panel interno (`j2ys-panel`)

`https://panel.3b-technology.com` → Cloudflare Access (login) → Worker de
Cloudflare → servicio de Cloud Run `j2ys-panel` → worker privado.

`j2ys-panel` (`app/services/panel_entry.py`) sirve el front (`frontend.git`,
carpeta `frontend/`) y reenvía `/api/v1/admin/*` al worker con la API key
(secreto `admin-api-key`) y un token OIDC de `j2ys-lt-run`: el navegador no ve
ninguno. Los `/admin/jobs/*` no pasan. Cada request debe traer el JWT de
Cloudflare Access (header `Cf-Access-Jwt-Assertion` o cookie
`CF_Authorization`); el panel verifica firma, `aud` y emisor, y sin él responde
403, también a quien entre directo por la URL `run.app`. El cambio de estado
del bot queda firmado con el correo del JWT.

Hecho (2026-10-20): secreto `admin-api-key` (lo lee `j2ys-lt-run`).

Una sola vez, en Cloudflare:
1. Zero Trust → Access → Applications → *Self-hosted*: dominio
   `panel.3b-technology.com`, política *Allow* con los correos del equipo.
   Anotar el *team domain* (`<equipo>.cloudflareaccess.com`) y el *AUD tag*.
2. Workers → crear `j2ys-panel` con este código y la ruta
   `panel.3b-technology.com/*` (más un registro DNS `panel` proxied; un
   AAAA a `100::` sirve si no hay otro):
   ```js
   export default {
     fetch(request) {
       const url = new URL(request.url);
       url.hostname = "j2ys-panel-ywi3275i5a-uc.a.run.app";
       return fetch(new Request(url, request));
     },
   };
   ```

Desplegar (cada cambio del panel o del front):
```bash
CF_ACCESS_TEAM_DOMAIN=<equipo>.cloudflareaccess.com CF_ACCESS_AUD=<aud>   FRONT_BRANCH=main ./ops/deploy_panel.sh
```
La URL abre la lista de negocios; `?business=<id>` abre uno.
