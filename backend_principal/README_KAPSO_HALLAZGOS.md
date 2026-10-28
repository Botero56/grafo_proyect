# Viabilidad: migrar de la WhatsApp Cloud API directa (Meta) a Kapso

> Investigación del 2026-10-12 · rama `feat/adding/signup_embedded` (HEAD `b594cc4`).
> Alcance: **solo lectura**. No se tocó código, configuración, IAM, secretos, colas ni despliegues.
> El único archivo creado es este README. Además, Graphify regeneró sus propias salidas en `graphify-out/` (que git ignora), porque así lo pedía el Paso 1.
> Nota: el enunciado de la tarea llegó cortado (el punto 9 del Paso 3 y la sección "ver al final" sobre el README no aparecen). Por eso el formato y la ubicación de este archivo son una decisión propia.

---

## 0. Resumen ejecutivo

| Pregunta | Respuesta corta |
|---|---|
| ¿Es técnicamente viable? | **Sí, y barato en código.** Kapso expone una API que copia la Graph API (`https://api.kapso.ai/meta/whatsapp/v24.0/{phone_number_id}/messages`) y puede reenviar los webhooks **en el formato crudo de Meta** (`kind: "meta"`). El backend apenas cambia. |
| ¿Cuánto código cambia? | Unas **~40 líneas en el núcleo**: URL base y header de auth en `WhatsAppClient`, verificación de firma y config. Hay que **reemplazar el módulo de onboarding** (~860 líneas: Embedded Signup propio, intercambio de token, `subscribed_apps`, `smb_app_data`) por los *setup links* de Kapso y un webhook de "número conectado". |
| ¿Resuelve el bloqueo de la verificación de Meta? | **Probablemente sí.** Con la app de Meta de Kapso, la doc dice textualmente: *"No Meta app or Tech Provider verification"*. Falta confirmar con Kapso que tampoco exigen nuestro portafolio verificado (no lo dicen ni a favor ni en contra). |
| ¿Coexistencia? | **Sí.** `allowed_connection_types` trae `coexistence` por defecto (20 msg/s). Los ecos de la app llegan con `origin: "business_app"`. Falta verificar en la práctica que con `kind: "meta"` llegue `smb_message_echoes`, porque de eso depende nuestro handoff. |
| ¿Pago centralizado? | **Sí, con matices.** `meta_billing_mode: "partner_managed"`: Kapso pone su línea de crédito en la WABA de cada cliente y descuenta de nuestros créditos prepagos en USD, a precio Meta, sin markup si la WABA está en USD. **Pero:** (a) la página de precios lo anuncia como característica del plan **Platform (USD 299/mes)** y la doc técnica no aclara si funciona en Pro; (b) si la WABA ya tiene tarjeta, Meta rechaza la línea de Kapso; (c) volver de la línea de crédito a una tarjeta exige una WABA nueva y migrar el número. |
| ¿Riesgos principales? | Proveedor joven y pequeño (Kapso, Inc., fundada en 2024; afiliada en Chile). No encontré un SLA publicado. Los reintentos de webhook son cortos (3 intentos en ~50 s). Kapso **guarda los mensajes y la media indefinidamente** por defecto, lo que es delicado para la odontología. El lock-in de facturación pesa más que el de código. |
| Recomendación | **Piloto con 1 número** (restaurante, poco tráfico) antes de mover a los 3 clientes. Antes del piloto, confirmar por escrito con Kapso los 6 puntos de la §6. |

---

## 1. Paso 1 — Grafo del proyecto (Graphify) y mapa de acoplamiento con Meta

### 1.1 Actualización del grafo

- `graphify --update .` → el detector marcó 338 archivos como nuevos o cambiados. La extracción AST cubrió el código (1446 nodos y 5258 aristas). La extracción semántica cubrió los 5 documentos de proyecto que importan aquí: `HANDOFF.md`, `ONBOARDING_COEXISTENCIA.md`, `README.md`, `SESION_2026-09-21_22.md` y `onboarding.html`.
- Excluí a propósito de la extracción semántica: los 37 `loadtest_results/*/report.html`, los docs de skills en `.claude/` y una imagen de prueba. No aportan al acoplamiento con Meta y quedan pendientes para un próximo `--update`.
- Resultado: **1633 nodos, 4676 aristas, 87 comunidades** en `graphify-out/graph.json`, `GRAPH_REPORT.md` y `graph.html`. Comunidades relevantes: *WhatsApp Cloud API Client*, *Webhook Ingress & Payload*, *Meta Graph API (Onboarding)*, *Embedded Signup Service*, *Coexistence Webhook Handling*, *WhatsApp Credentials Schema*, *Cloud Tasks Client*, *Handoff & Agent Runner*.
- Pendiente técnico: en Windows, `extract()` falla si el script se pasa por stdin (multiprocessing `spawn`). Hay que ejecutarlo desde un archivo `.py`. El comando `graphify` de `~/.local/bin` da *Permission denied*; `python -m graphify` sí funciona.

### 1.2 Hallazgo estructural clave

**Todo el envío pasa por un único punto: `WhatsAppClient.for_business()`.** Lo usan 12 módulos: router, handoff, menú diario, recordatorios, pagos, domicilios, audio, etc. Ninguno arma URLs de Meta por su cuenta. La vertical dental (`app/dental`) **no tiene acoplamiento directo** con WhatsApp: usa el pipeline compartido. Gracias a eso, el cambio de proveedor de envío es local.

### 1.3 Puntos de acoplamiento

Nivel = cuánto cambia si pasamos a Kapso **con webhooks `kind: "meta"`** (en paréntesis, el nivel si se usara el formato propio de Kapso v2).

| # | Punto | Archivo · función | Qué hace hoy | Cambio con Kapso | Nivel |
|---|---|---|---|---|---|
| 1 | Verificación GET del webhook | `app/services/webhook_entry.py` · `verify_webhook` (L72) | Handshake `hub.mode` / `hub.verify_token` / `hub.challenge` con `VERIFY_TOKEN` | Kapso no documenta handshake GET; el endpoint queda sin uso, sin daño | Bajo |
| 2 | Firma del webhook | `app/services/whatsapp_client/signature.py` · `verify_meta_signature`; usada en `webhook_entry.receive_webhook` (L93) y `router.receive_webhook` (L98) | HMAC-SHA256 del body crudo con `APP_SECRET`, header `X-Hub-Signature-256`, prefijo `sha256=` | Header `X-Webhook-Signature`, hex **sin** prefijo `sha256=` (en `kind: meta`), secreto = `secret_key` que definimos al crear el webhook. Unas 10 líneas | **Medio** |
| 3 | Formato del payload | `app/services/messaging/webhook_payload.py` · `split_events`, `event_id`, `event_field`, `split_history`, `content_event_id` | Separa `entry` → `changes` → `value` → `messages` / `message_echoes` y saca el `wamid` | Igual con `kind: meta` (Kapso: *"Receive events in Meta's payload format"*, sin batching) | Bajo (Alto en v2) |
| 4 | Parseo de mensajes entrantes | `app/services/firestore_services/firestore_webhook_service.py` · `process_incoming_payload` (L68), `_extract_message_content` (L289), `find_business` (L18) | Lee `contacts`, `metadata.display_phone_number` / `phone_number_id`, `interactive.button_reply`, `media_id` | Igual con `kind: meta` | Bajo (Alto en v2) |
| 5 | Router de eventos de coexistencia | `app/services/firestore_services/webhook_event_router.py` · `route` (L45), `_process_echo` (L74), `_process_history` (L196), `_process_app_state_sync` (L324) | Clasifica por `changes[0].field`: `smb_message_echoes`, `history`, `smb_app_state_sync` | Depende de que Kapso reenvíe esos `field` crudos (ver §6, P2). En v2 serían `whatsapp.message.sent` con `origin: business_app` / `history_sync` | **Medio** (Alto en v2) |
| 6 | Estados (`statuses`) | `webhook_entry._log_failed_statuses` (L50), `firestore_webhook_service._handle_status_update` (L318) | Solo registra en el log los `failed` | Sin cambio en `kind: meta` | Bajo |
| 7 | Idempotencia y encolado | `webhook_entry.receive_webhook` (L87) + `app/services/tasks/cloud_tasks_client.py` · `enqueue_event`, `enqueue_sync_event`, `_task_name` | Nombre de tarea determinista = `wamid` o hash. Si falla el encolado, responde 502 para que Meta reintente | La lógica sirve. Kapso añade `X-Idempotency-Key`. **Ojo con la política de reintentos** (ver §3.2) | Medio |
| 8 | Envío de mensajes | `app/services/whatsapp_client/whatsapp_client.py` · `WhatsAppClient.__init__` (L96, `https://graph.facebook.com/{v26.0}`), `_send` (L163, `POST {phone_number_id}/messages`), `send_message`, `send_text_and_get_id`, `send_document`, `send_interactive_buttons`, `send_template` | Bearer token, payloads Cloud API, reintentos 429/5xx | URL base `https://api.kapso.ai/meta/whatsapp/v24.0` y header `X-API-Key` (también acepta `Authorization: Bearer`). Los payloads no cambian. Verificar la forma de la respuesta (`messages[0].id`) | **Medio-bajo** |
| 9 | Versión de la Graph API | `app/config.py` · `WhatsAppsettings.GRAPH_API_VERSION = "v26.0"` | — | Kapso fija `v24.0` en la ruta. No usamos nada exclusivo de v26 | Bajo |
| 10 | Media: subir y descargar | `WhatsAppClient.upload_media` (`POST {pnid}/media`), `get_media_metadata` (`GET /{media_id}`), `download_media` (GET a la URL temporal con el mismo Bearer). Usado por `audio_processor.py`, `transfer_receipt.py`, `daily_menu/media_downloader.py`, `router._send_pending_document` | — | Kapso tiene `POST /{pnid}/media`, `GET /{media_id}` (URL válida 5 min) y `GET /media_download?token=…` sin versión. Probablemente funcione tal cual; verificar el header en la descarga | Medio-bajo |
| 11 | Plantillas | `send_template` + `config.DAILY_MENU_REMINDER_TEMPLATE`, `HANDOFF_NOTIFY_TEMPLATE` (hoy **vacías** en Cloud Run) | Solo se envían. No se gestionan desde el código | Igual. Las plantillas pertenecen a la WABA de cada cliente: hay que crearlas o sincronizarlas por cliente (`/{waba_id}/message_templates`) | Bajo |
| 12 | Tokens y credenciales | `config.WhatsAppsettings` (`TEMP_TOKEN_WPP`, `PHONE_NUMBER_ID`, `APP_SECRET`, `VERIFY_TOKEN`); `whatsapp_client._credential_token` (token por número, caché de 5 min); `firebase_schemas.WhatsappCredentials` (`access_token` **en texto plano** en Firestore, colección `whatsapp_credentials`) | Token de usuario del sistema global + token de onboarding por número | Se reemplaza todo por **una API key de proyecto Kapso**. `_credential_token` y el `access_token` en Firestore dejan de ser necesarios | **Medio** |
| 13 | Embedded Signup propio | `app/services/onboarding/routes.py` (página + `POST /api/whatsapp/onboarding`), `onboarding_service.complete_onboarding` (7 pasos), `graph_api.py` (`exchange_code` → `oauth/access_token`, `find_phone_number_id`, `waba_phone_number_ids`, `get_phone_number`, `get_coexistence_status`, `subscribe_app` → `/{waba}/subscribed_apps`, `request_smb_sync` → `/{pnid}/smb_app_data`), `static/onboarding.html` (FB SDK, `META_APP_ID`, `META_CONFIG_ID`) | Onboarding completo con nuestra app de Meta | **Se reemplaza** por setup links de Kapso (`POST /platform/v1/customers/{id}/setup_links`) y el evento `whatsapp.phone_number.created`. La spec de Kapso **no expone `smb_app_data`** | **Alto** (pero es reemplazo, no reescritura) |
| 14 | Sincronización de coexistencia | `app/services/onboarding/coexistence_sync.py` · `run_onboarding_sync`; worker `/tasks/onboarding-sync`; `cloud_tasks_client.enqueue_onboarding_sync`; cola `whatsapp-history` | Pide a Meta contactos e historial (24 h de plazo) | La sincronización la pediría Kapso, que no la documenta (ver §6, P3) | Alto |
| 15 | Handoff al administrador | `app/services/conversation/handoff_service.py` · `pause_bot`, `reactivate_bot`, `should_auto_reactivate`, `notify_admins` (L152, texto o plantilla); `HandoffSettings` | Pausa el bot cuando llega un **echo** (la encargada escribe desde el celular) y lo reactiva tras `HANDOFF_QUICK_REACTIVATION_SECONDS` = 30 | Lógica intacta **si** llegan los echoes. Es el punto más crítico para el negocio | **Medio** (crítico) |
| 16 | Resolución de negocio por número | `find_business(display_phone_number, phone_number_id)`; `Businesses.whatsapp_phone_number_id`, `waba_id`; `admin/businesses_router.py` | Busca por el `phone_number_id` de Meta | Kapso enruta por `phone_number_id`. Verificar que sea el mismo ID de Meta (todo indica que sí: aparece igual en la ruta de la API) | Bajo |
| 17 | Logs de errores de Meta | `WhatsAppClient._log_api_error` | Lee `error.code` / `error_subcode` (190, 131005, 131047…) | Kapso puede envolver los errores de otra forma; solo afecta al diagnóstico | Bajo |

---

## 2. Paso 2 — Infraestructura en GCP (solo lectura)

Proyecto `j2ys-tech-chat-automation`, cuenta `juanbotero280080@gmail.com`. Ningún otro proyecto accesible tiene servicios de Cloud Run de j2ys.

### 2.1 Servicios de Cloud Run (región `us-central1`)

| | **`j2ys-lt-plain`** = webhook | **`j2ys-lt-app`** = worker |
|---|---|---|
| `SERVICE_ROLE` | `webhook` → `app.services.webhook_entry` | `worker` → `app.services.worker_entry` |
| URLs | `https://j2ys-lt-plain-ywi3275i5a-uc.a.run.app` · `https://j2ys-lt-plain-435766564352.us-central1.run.app` | `https://j2ys-lt-app-ywi3275i5a-uc.a.run.app` · `…-435766564352.us-central1.run.app` |
| Ingress | `all` | `all` |
| Autenticación (IAM invoker) | `allUsers` (**público**, correcto para Meta) | solo `j2ys-lt-run@…` (privado por IAM, OIDC desde Cloud Tasks) |
| Cuenta de servicio | `j2ys-lt-run@j2ys-tech-chat-automation.iam.gserviceaccount.com` | ídem |
| Escalado / recursos | min 0 · **max 1** · 1 vCPU · 1 GiB · concurrencia 40 · timeout 300 s | ídem (max 1 por los locks en memoria, ver `worker_entry.py`) |
| Última revisión | `j2ys-lt-plain-00031-rrs` (2026-10-11 03:10 UTC) | `j2ys-lt-app-00057-h6h` (2026-10-11 03:10 UTC) |

**Variables de entorno relevantes** (no se muestran valores secretos):

- Meta: `TEMP_TOKEN_WPP` (secreto, en ambos), `APP_SECRET` (secreto, en ambos), `VERIFY_TOKEN` (secreto; **distinto** entre servicios, solo importa el del webhook), `PHONE_NUMBER_ID=1295952196934414` (número global de respaldo), `GRAPH_API_VERSION=v26.0`, `GRAPH_API_BASE_URL=https://graph.facebook.com` (**el código no la lee**: la URL está fija en `whatsapp_client.py` y `graph_api.py`).
- Onboarding (solo en el webhook): `META_APP_ID=2103790087686306`, `META_CONFIG_ID` (definido), `META_ES_COEXISTENCE_FEATURE_TYPE=true`.
- Cloud Tasks: `CLOUD_TASKS_QUEUE=whatsapp-messages`, `CLOUD_TASKS_HISTORY_QUEUE=whatsapp-history`, `CLOUD_TASKS_LOCATION=us-central1`, `WORKER_URL=https://j2ys-lt-app-ywi3275i5a-uc.a.run.app`, `WORKER_SERVICE_ACCOUNT_EMAIL=j2ys-lt-run@…`.
- Otras: `PROJECT_ID`, `DATABASE=j2ys-tech-core`, `MODEL_RESTAURANT=gemini-3.8-flash` (en ambos; `MODEL_DENTIST` se borró el 2026-10-17), `API_KEY` (secreto), `PRINT_URL`, `RECEIPTS_BUCKET` (solo worker).

**Observaciones (no son cambios, solo hallazgos):**
- **Secret Manager está vacío.** Todos los secretos (`TEMP_TOKEN_WPP`, `APP_SECRET`, `VERIFY_TOKEN`, `API_KEY`) son variables de entorno en texto plano, visibles para cualquiera con `run.services.get`. Con Kapso habría un secreto nuevo (API key de Kapso + `secret_key` del webhook). Es buen momento para moverlos a Secret Manager (pendiente, fuera de este alcance).
- El webhook lleva variables que no necesita (`MODEL_*`, `PRINT_URL`). `TEMP_TOKEN_WPP` sí puede necesitarlo, por el onboarding que vive en ese servicio.
- Logs: las últimas 500 peticiones `POST /webhook` desde el 2026-10-05 respondieron todas **200**.

### 2.2 Cloud Tasks (`us-central1`)

| Cola | Estado | Máx. despachos/s | Máx. concurrentes | Máx. intentos | Backoff máx. |
|---|---|---|---|---|---|
| `whatsapp-messages` | RUNNING | 500 | 1000 | 100 (el worker corta en 5 con `_MAX_RETRIES`) | 3600 s |
| `whatsapp-history` | RUNNING | 5 | 5 | 10 | 300 s |

Cadena actual: **Meta → `POST /webhook` (j2ys-lt-plain)** → verifica la firma → `split_events` → `enqueue_event` (nombre = `wamid`) o `enqueue_sync_event` (history / app_state → cola `whatsapp-history`) → **Cloud Tasks (OIDC)** → `POST /tasks/process-event` (j2ys-lt-app) → ingesta → `enqueue_flush` con `schedule_time` = +5 s → `/tasks/flush-conversation` → IA → `WhatsAppClient` → Graph API. El onboarding agrega `/tasks/onboarding-sync`.

### 2.3 Qué cambiaría si el webhook recibe eventos de Kapso

1. **URL:** puede ser **la misma** (`https://j2ys-lt-plain-…/webhook`). En Kapso el webhook `kind: "meta"` se registra **por número de teléfono** (uno por número, como máximo). Hay que registrarlo en cada número de cliente, desde el dashboard o la API, idealmente al recibir `whatsapp.phone_number.created`.
2. **Verificación:** se cambia `X-Hub-Signature-256` + `APP_SECRET` por `X-Webhook-Signature` + `secret_key` (hex sin `sha256=`). Desaparece el handshake GET.
3. **Formato:** con `kind: "meta"` no cambia. Con eventos Kapso v2 habría que reescribir los puntos 3 a 6 de la tabla.
4. **Plazos:** Kapso exige 200 en **≤10 s**. Nuestro webhook solo encola, así que cumple. Pero Kapso reintenta **solo 2 veces (a los 10 s y a los 40 s)**, y Meta lo hace durante horas o días. Si Cloud Tasks falla más de ~50 s, el mensaje se pierde, porque el 502 deja de servir de red de seguridad. Además, un 85 % de fallos con ≥40 entregas en 15 min **pausa el webhook** automáticamente.
5. **Nuevo endpoint (opcional):** un receptor del *project webhook* de Kapso para `whatsapp.phone_number.created` (trae `phone_number_id`, `customer.id`, `project.id`; **no trae el WABA ID**, que solo llega en el redirect de éxito si está disponible). Reemplaza al `POST /api/whatsapp/onboarding`.
6. **Cola `whatsapp-history` y `/tasks/onboarding-sync`:** se conservan si Kapso reenvía `history` / `smb_app_state_sync`. Si no, pierden uso.
7. **Ingress / IAM:** sin cambios (el webhook sigue público y el worker privado).

---

## 3. Paso 3 — Investigación sobre Kapso

Fuentes principales: [kapso.com](https://kapso.com/), [docs.kapso.ai](https://docs.kapso.ai) ([índice llms.txt](https://docs.kapso.ai/llms.txt)), [precios](https://kapso.com/pricing). El sitio **no tiene changelog, blog ni GitHub enlazados** desde la portada. Sí enlaza specs OpenAPI ([WhatsApp](https://docs.kapso.ai/api/meta/whatsapp/openapi-whatsapp.yaml), [Platform](https://kapso.com/openapi/platform.json)), un CLI, un servidor MCP y un SDK (`@kapso/whatsapp-cloud-api`).

### 3.1 Modelo de la API

- La API de WhatsApp *"Mirrors Meta's Graph API shapes, so existing Cloud API code ports over with a base URL change"*. Base: `https://api.kapso.ai/meta/whatsapp/v24.0`. Auth: `X-API-Key` (recomendado) o `Authorization: Bearer` (compatibilidad). [Fuente](https://docs.kapso.ai/api/introduction.md)
- Rutas de la spec relevantes para nosotros: `/{pnid}/messages` (GET para listar y POST para enviar texto, media, interactivos, plantillas, reacciones y marcar como leído), `/{pnid}/media`, `/{media_id}`, `/media_download`, `/{waba_id}/message_templates`, `/{waba_id}/flows`, `/{pnid}/whatsapp_business_profile`, `/{pnid}/block_users`, `/{waba_id}/phone_numbers`, `/{pnid}`, `/{pnid}/thread_control`, `/{pnid}/calls`. **No hay** `smb_app_data`, `subscribed_apps` ni `oauth/access_token`: eso lo gestiona Kapso.
- Advertencia: *"Most responses wrap results in a `data` object"* (aplica a la Platform API; para `/messages` dicen que se mantiene la forma de Meta). **Verificar** que `POST /messages` devuelva `messages[0].id`.
- **Código a reescribir:** `WhatsAppClient` (URL base + header), `signature.py`, `config.py` (nuevas variables `KAPSO_API_KEY`, `KAPSO_WEBHOOK_SECRET`, base URL) y simplificar `_credential_token`. El resto del backend no cambia. El onboarding se reemplaza.

### 3.2 Webhooks

- Dos tipos: **project webhooks** (ciclo de vida de la conexión, por ejemplo `whatsapp.phone_number.created`; *"No message or conversation events here"*) y **WhatsApp webhooks por número**. Estos últimos pueden ser **Kapso v2** (eventos `whatsapp.message.received/sent/delivered/read/failed`, `whatsapp.conversation.*`, `whatsapp.thread.*`, con buffering opcional) o **Meta** (`kind: "meta"`, payload de Meta, *"Kapso forwards all Meta webhook events"*, sin filtros ni batching, `secret_key` obligatorio, uno por número). [Overview](https://docs.kapso.ai/docs/platform/webhooks/overview.md) · [Eventos](https://docs.kapso.ai/docs/platform/webhooks/message-events.md)
- Firma: HMAC-SHA256 hex del body crudo en `X-Webhook-Signature`. En `kind: meta` va sin prefijo `sha256=`. Headers adicionales: `X-Idempotency-Key` y, en v2, `X-Webhook-Event`, `X-Webhook-Payload-Version: v2`, `X-Webhook-Batch`. [Seguridad](https://docs.kapso.ai/docs/platform/webhooks/security.md)
- Reintentos: inmediato y luego a los 10 s y 40 s; `max_attempts` = 3; responder 200 en ≤10 s. Auto-pausa con ≥40 entregas, ≥10 fallidas y ≥85 % de fallos en 15 min. Orden garantizado por conversación hasta 30 s. [Avanzado](https://docs.kapso.ai/docs/platform/webhooks/advanced.md)
- Echoes en v2: `message.kapso.origin` ∈ `cloud_api` | `business_app` (*"Echoed from WhatsApp Business App"*) | `history_sync` | `other_app` | `meta_business_agent`.

### 3.3 Onboarding / Embedded Signup

- **Setup links alojados por Kapso:** `POST /platform/v1/customers/{customer_id}/setup_links` → `url` para enviar al cliente. En el ejemplo de la doc, la URL vence a los 30 días. Flujo del cliente: *"Send a link, customer clicks, logs in with Facebook, and you're connected"*, en ~5 min. [Crear y configurar](https://docs.kapso.ai/docs/platform/setup-links/create-and-configure.md) · [Guía del cliente](https://docs.kapso.ai/docs/platform/customer-guide.md)
- Opciones: `allowed_connection_types` (`["coexistence","dedicated"]` por defecto; con un solo valor se fuerza), `success_redirect_url` / `failure_redirect_url`, `language` (incluye `es`), `theme_config`, `meta_billing_mode` (`customer_managed` por defecto, o `partner_managed`), `reconnect_phone_number`, `provision_phone_number` (números nuevos; fuera de EE. UU. requiere telefonía propia).
- Detección: evento `whatsapp.phone_number.created` o redirect con `phone_number_id`, `business_account_id` (si está disponible) y `display_phone_number`. [Detectar conexión](https://docs.kapso.ai/docs/platform/setup-links/detect-connection.md)
- **¿El cliente crea cuenta?** No en Kapso: solo entra con Facebook y completa el Embedded Signup de Meta, que crea o elige el portafolio y la WABA. Es lo mismo que hoy con nuestra app.
- **¿Sirve sin portafolio verificado nuestro?** La ruta "app de Meta de Kapso" dice *"No Meta app or Tech Provider verification"*. [Tech providers](https://docs.kapso.ai/docs/platform/tech-providers/overview.md). No dice nada explícito sobre la verificación de **nuestro** portafolio. Es razonable suponer que no hace falta, porque la WABA es del cliente y el Tech Provider es Kapso, pero **hay que confirmarlo** (P1). Las otras dos rutas (app propia + Multi-partner Solution, o app propia sin MPS) exigen ser Tech Provider, que es justo lo que no tenemos.
- Por confirmar: qué nombre ve el cliente en el diálogo de Meta (probablemente "Kapso").

### 3.4 Coexistencia

- Soportada (`coexistence` en `allowed_connection_types`, 20 msg/s frente a 1000 en `dedicated`).
- Limitaciones documentadas por Kapso: desconexiones ocasionales, problemas de sincronización con WhatsApp Web y con nombres de contactos, algunos primeros mensajes entrantes pueden no llegar de inmediato, **sin llamadas por Kapso** (en la app sí funcionan), y el nombre visible se gestiona en la app. Siguen aplicando las restricciones de Meta (nombre visible, verificación, pagos, plantillas). [Troubleshooting](https://docs.kapso.ai/docs/how-to/whatsapp/coexistence-troubleshooting.md)
- **No documentado por Kapso:** sincronización de historial y contactos (solo se menciona `origin: history_sync` *"only if the project ran sync"*), grupos, mensajes temporales o de una sola vista, difusiones.
- Otras fuentes: los grupos no se sincronizan en coexistencia ([Chakra](https://chakrahq.com/article/whatsapp-coexistence-live-eu-uk-europe-whatsapp-business-for-api-live/)). Hay que abrir la app al menos cada 14 días para no perder la coexistencia ([seven.io](https://help.seven.io/en/whatsapp/whatsapp-coexistence)). En otros proveedores, `history` y `smb_app_state_sync` no se reenvían al cliente ([Telnyx](https://developers.telnyx.com/docs/messaging/whatsapp/coexistence/webhooks)), y hay reportes de echoes que no llegan cuando la suscripción queda en la app del proveedor ([comunidad Make](https://community.make.com/t/whatsapp-coexistence-watch-events-does-not-expose-smb-message-echoes/114713)). Los echoes no abren la ventana de 24 h ni se cobran (Telnyx).
- **Colombia:** ni Kapso ni las listas de exclusión de terceros mencionan a Colombia. Esas listas, hoy casi vacías, cubrían UE/UK, India, Japón, Australia, Nigeria, Sudáfrica, Turquía, Rusia, Corea y Filipinas ([360dialog](https://docs.360dialog.com/partner/waba-management/phone-number-and-hosting/using-whatsapp-app-and-cloud-api-simultaneously)). Además, nuestro propio flujo de coexistencia ya está desplegado con números colombianos. → **Colombia: soportado.**

### 3.5 Facturación

- **Planes de Kapso** ([precios](https://kapso.com/pricing)):

  | Plan | USD/mes | Mensajes/mes (entrantes **y** salientes cuentan) | Números | Número extra |
  |---|---|---|---|---|
  | Free | 0 | 2000 | 1 | — |
  | Pro | 25 | 100 000 | 3 | 10 |
  | Platform | 299 | 1 000 000 | 50 | 5 |
  | Enterprise | a medida | | | |

  En todos los planes: inbox, webhooks, plantillas, Flows, CLI/MCP/SDK. Límite de API: 100 / 1000 / 2000 / 4000 req/min (Free / Pro / Platform / Enterprise) ([rate limits](https://docs.kapso.ai/api/rate-limits.md)). No hay sobrecosto por mensaje publicado ([FAQ](https://docs.kapso.ai/docs/whatsapp/pricing-faq.md)).
- **Las tarifas de Meta van aparte.** Se pagan a través de Kapso (sin markup) o directamente a Meta.
- **`partner_managed` (pago centralizado):** Kapso pone su línea de crédito en la WABA del cliente, paga a Meta y descuenta de **nuestros créditos prepagos en USD**. WABA en USD: precio Meta publicado. WABA en otra moneda (por ejemplo COP): +5 % FX, y la moneda tiene que estar habilitada o falla con `unsupported_currency`. Si la conexión pasa por la infraestructura propia (MPS), se suma un 5 %. [Billing TP](https://docs.kapso.ai/docs/platform/tech-providers/billing.md) · [Meta billing](https://docs.kapso.ai/docs/whatsapp/meta-message-billing.md)
- **Matices críticos:**
  1. **¿Pro o Platform?** La página de precios lista *"Centralized Meta billing for all your customers"* solo en **Platform**. La doc técnica no exige un plan para `partner_managed` con la app de Kapso. **Ambiguo → confirmar (P4).** Si exige Platform, el costo base pasa de 25 a 299 USD/mes.
  2. Si la WABA del cliente **ya tiene tarjeta** en Meta, Meta rechaza la línea de Kapso y la WABA queda en `customer_managed`. Para nuestros 3 clientes actuales hay que saber si su WABA tiene método de pago. Si lo tiene, conviene que el setup link cree una WABA nueva.
  3. **Lock-in:** *"Meta does not currently support switching an existing WABA from a partner shared credit line to a card"*. Salir de Kapso exige WABA nueva + migrar el número.
  4. Sin créditos, se pausan los envíos potencialmente pagos en todos los números de la WABA (`service_credits_exhausted`). Necesitamos alertas de saldo.
- **Cambio de Meta que nos afecta haya o no Kapso:** según Kapso, *"non-template messages have been a billable service category since October 1, 2026"*, con **1000 mensajes de servicio entregados gratis por número al mes**. Con 3 clientes, las respuestas del bot por encima de 1000/mes por número ahora tienen costo. [FAQ](https://docs.kapso.ai/docs/whatsapp/pricing-faq.md) (verificar con la fuente oficial de Meta).
- Ojo con el cupo de Kapso: como cuentan entrantes, salientes y mensajes pasivos de la app, 100 000/mes en Pro alcanzan para 3 negocios, pero los mensajes que la encargada escribe desde la app **también cuentan**.

### 3.6 Multi-cliente

- Modelo: un **proyecto** Kapso (nuestro) con N **customers** (`/platform/v1/customers`, un CRUD) y setup links por customer. Cada negocio conecta **su propia WABA** con su número y sus plantillas. [Customers API](https://docs.kapso.ai/api/platform/v1/customers/list-customers.md)
- Enrutamiento por `phone_number_id`, como hoy (`Businesses.whatsapp_phone_number_id`).
- **Aislamiento:** a nivel de Meta lo da que cada cliente tenga su WABA. A nivel de Kapso hay **una sola API key de proyecto** para todos los números, así que el aislamiento en nuestro backend sigue siendo nuestra responsabilidad (`TenantScopedQuery`, chequeo de `business_id` frente a `phone_number_id`). Hoy cada número tiene su propio token; con Kapso, un error de `phone_number_id` podría enviar desde el número de otro cliente. Conviene mantener la validación de propiedad antes de enviar.
- La facturación es por WABA, así que el costo por cliente se puede separar.

### 3.7 Plantillas, media, interactivos y Flows

| Capacidad | Meta directo (hoy) | Kapso |
|---|---|---|
| Texto, documento, botones (`interactive.button`), plantilla | ✔ | ✔ (mismo payload) |
| Listas, ubicación, reacciones, marcar como leído | (no se usan) | ✔ |
| Subir y descargar media | ✔ | ✔ (`/{pnid}/media`, `/{media_id}`, `/media_download`) |
| Gestión de plantillas | Business Manager | ✔ `/{waba_id}/message_templates` + "Sync from WhatsApp" |
| WhatsApp Flows | — | ✔ `/{waba_id}/flows` y siguientes |
| Llamadas | — | ✔ API, **pero no en coexistencia** |
| `smb_app_data` (pedir la sincronización) | ✔ | ✘ (lo gestiona Kapso, sin documentar) |
| Transcripción automática de audios | propia (`audio_processor.py`) | ✔ `message.kapso.transcript` (en v2) |

### 3.8 Producción y riesgos

- **Empresa:** Kapso, Inc. (Delaware, EE. UU.) con afiliada Kapso SpA (Chile). Fundada en 2024 y respaldada por la aceleradora Platanus; según Dealroom, la creó un fundador en solitario (Andrés Matte), con una semilla de 1,4 M USD que no pude verificar. [Privacidad](https://kapso.com/privacy) · [Dealroom](https://app.dealroom.co/companies/kapso) · [startup-seeker](https://startup-seeker.com/company/kapso~com)
- **Estatus con Meta:** se presenta como *"Official Meta Business Partner"* y puede pagar a Meta por clientes (*"Only BSPs can pay Meta for their customers' WhatsApp usage"*), lo que implica que es BSP con línea de crédito. **Verificarlo en el directorio oficial de partners de Meta** (P6).
- **SLA:** no encontré SLA ni página de estado publicados. Enterprise menciona "dedicated support".
- **Límites:** 1000 req/min en Pro, además de los límites propios de Meta (coexistencia: 20 msg/s). Para nuestro volumen sobra.
- **Lock-in:** en **código es bajo**, porque mantenemos la forma de la Graph API y el formato de webhook de Meta. En **facturación es alto** (§3.5, punto 3). Las plantillas pertenecen a la WABA, no a Kapso.
- **Portabilidad para volver a Meta directo:** Kapso no documenta la salida. Como la WABA es del cliente, volver implicaría que nuestra app (ya verificada) haga un Embedded Signup nuevo sobre el número. En coexistencia, el número sigue en la app del negocio. Con `partner_managed` habría que crear una WABA nueva. Plan B a validar.
- **Privacidad / datos:** Kapso **almacena contenido, media y transcripciones**, por defecto **sin límite de tiempo**. En planes pagos se puede fijar una ventana de retención; el log de búsqueda dura 30 días. No publica la región de almacenamiento (*"providers that operate in the United States and other countries"*). Existe un DPA (pedirlo a legal@kap.so). [Datos](https://docs.kapso.ai/docs/platform/whatsapp-data.md). **Para la odontología (datos de salud) esto es un punto sensible:** el memo del proyecto pide mantener los datos médicos separados. Hay que fijar la retención mínima y revisar el DPA y los subprocesadores antes de pasar la clínica.
- **Soporte:** dev@kap.so. Inbox y Kapso Agent en el dashboard.

---

## 4. Estimación del cambio en nuestro código (sin ejecutar)

| Bloque | Esfuerzo | Detalle |
|---|---|---|
| `WhatsAppClient` | S | URL base y header configurables (aprovechar la `GRAPH_API_BASE_URL` que hoy existe y no se usa). Quitar o simplificar `_credential_token` |
| `signature.py` + `receive_webhook` (×2) | S | Header y prefijo configurables según el proveedor |
| `config.py` | S | `KAPSO_API_KEY`, `KAPSO_WEBHOOK_SECRET`, `WHATSAPP_PROVIDER=meta|kapso` (para poder volver atrás) |
| Onboarding | M | Página nueva: crea el customer y el setup link y redirige. Receptor de `whatsapp.phone_number.created` → `_mark_business_connected` + registro del webhook `kind: meta` del número. Retirar `graph_api.py`, `coexistence_sync.py` y `/tasks/onboarding-sync` si Kapso hace la sincronización |
| Tests | M | `test_signature`, `test_webhook_http`, `test_whatsapp_client`, `test_onboarding`, `test_e2e_flow` (firmas y URLs) |
| Infra | S | Secretos nuevos (idealmente en Secret Manager) y despliegue de ambos servicios |

Todo esto queda **pendiente**: no se tocó nada.

---

## 5. Plan sugerido de migración (para decidir, no ejecutado)

1. Cuenta Kapso **Free** (1 número, 2000 mensajes) para la prueba técnica: envío, media, botones, plantilla, webhook `kind: meta`, echoes y firma. Usar un número de prueba en coexistencia.
2. Con las respuestas de la §6, elegir **Pro** o **Platform** según dónde funcione `partner_managed`.
3. Piloto con **1 restaurante** de poco tráfico durante 1–2 semanas, con un flag `WHATSAPP_PROVIDER` para volver atrás.
4. Segundo restaurante. **Odontología al final**, después de cerrar retención y DPA.
5. Definir alertas de saldo de créditos y monitorear las entregas fallidas de webhooks en Kapso.

---

## 6. Pendientes / verificaciones (requieren prueba o confirmación con Kapso; no se hicieron)

| # | Pendiente | Por qué importa | Cómo verificar |
|---|---|---|---|
| P1 | ¿La ruta "app de Meta de Kapso" + `partner_managed` funciona **sin portafolio verificado nuestro**? ¿Qué nombre ve el cliente en el diálogo? | Es el motivo de la migración | Preguntar a dev@kap.so; hacer la prueba en Free |
| P2 | ¿Con `kind: "meta"` llegan `smb_message_echoes`, `history` y `smb_app_state_sync` crudos? | El handoff depende de los echoes (`_process_echo`) | Número de prueba en coexistencia + escribir desde la app + revisar logs de `j2ys-lt-plain` |
| P3 | ¿Kapso pide la sincronización de historial y contactos (`smb_app_data`)? ¿Es configurable? | Hoy lo hace `coexistence_sync.py`, que no tendría endpoint en Kapso | Preguntar a Kapso; prueba de onboarding |
| P4 | ¿`partner_managed` requiere el plan **Platform** (299) o funciona en Pro (25)? | Multiplica por 12 el costo base | Preguntar a Kapso (la página de precios y la doc se contradicen) |
| P5 | ¿Las WABAs de nuestros 3 clientes tienen tarjeta propia? ¿En qué moneda están (COP o USD)? ¿Kapso tiene COP habilitado? | Con tarjeta no se puede centralizar; en COP falla con `unsupported_currency` o paga +5 % FX | Revisar Meta Billing Hub de cada cliente; preguntar a Kapso |
| P6 | Estatus oficial de Kapso como BSP / Tech Provider en el directorio de Meta; SLA, página de estado, región de datos, DPA y subprocesadores | Riesgo de proveedor y cumplimiento (datos de salud) | Directorio de partners de Meta; legal@kap.so |
| P7 | Forma de la respuesta de `POST /messages` (`messages[0].id`) y descarga de media con `X-API-Key` frente a Bearer | `_first_message_id` y `download_media` | Prueba con curl en Free |
| P8 | Registro del webhook `kind: meta` **por número** vía API (no solo desde el dashboard) | Automatizar el alta de clientes | Revisar la Platform API (`project-webhooks` / webhooks por número) |
| P9 | Política ante los reintentos cortos de Kapso: ¿responder siempre 200 y reencolar internamente? | Evitar perder mensajes si Cloud Tasks falla más de ~50 s | Decisión de diseño; requiere cambio de código |
| P10 | Que Meta cobre los mensajes de servicio desde el 2026-10-01 (1000 gratis por número al mes) | Afecta el costo con o sin Kapso | Confirmar en la documentación oficial de precios de Meta |
| P11 | Mover los secretos actuales a Secret Manager | Hoy están en texto plano en las variables de Cloud Run | Fuera de alcance (cambio de infra) |
| P12 | Completar la extracción semántica de Graphify de los docs excluidos (loadtest, skills) | Grafo completo | `graphify --update` en otra sesión |

---

## 7. Fuentes

- Kapso: [inicio](https://kapso.com/) · [precios](https://kapso.com/pricing) · [docs](https://docs.kapso.ai) · [llms.txt](https://docs.kapso.ai/llms.txt) · [introducción a la API](https://docs.kapso.ai/api/introduction.md) · [OpenAPI WhatsApp](https://docs.kapso.ai/api/meta/whatsapp/openapi-whatsapp.yaml) · [webhooks overview](https://docs.kapso.ai/docs/platform/webhooks/overview.md) · [eventos](https://docs.kapso.ai/docs/platform/webhooks/message-events.md) · [seguridad](https://docs.kapso.ai/docs/platform/webhooks/security.md) · [avanzado](https://docs.kapso.ai/docs/platform/webhooks/advanced.md) · [setup links](https://docs.kapso.ai/docs/platform/setup-links/create-and-configure.md) · [detectar conexión](https://docs.kapso.ai/docs/platform/setup-links/detect-connection.md) · [guía del cliente](https://docs.kapso.ai/docs/platform/customer-guide.md) · [coexistencia](https://docs.kapso.ai/docs/how-to/whatsapp/coexistence-troubleshooting.md) · [conversation routing](https://docs.kapso.ai/docs/whatsapp/conversation-routing.md) · [tech providers](https://docs.kapso.ai/docs/platform/tech-providers/overview.md) · [billing TP](https://docs.kapso.ai/docs/platform/tech-providers/billing.md) · [Meta message billing](https://docs.kapso.ai/docs/whatsapp/meta-message-billing.md) · [funding](https://docs.kapso.ai/docs/platform/whatsapp-funding.md) · [FAQ de precios](https://docs.kapso.ai/docs/whatsapp/pricing-faq.md) · [app propia](https://docs.kapso.ai/docs/platform/onboard-customers-own-meta-app.md) · [migrar desde otro proveedor](https://docs.kapso.ai/docs/migrate/from-another-provider.md) · [datos](https://docs.kapso.ai/docs/platform/whatsapp-data.md) · [rate limits](https://docs.kapso.ai/api/rate-limits.md) · [privacidad](https://kapso.com/privacy) · [conectar WhatsApp de clientes](https://kapso.com/connect-customer-whatsapp)
- Secundarias: [Dealroom](https://app.dealroom.co/companies/kapso) · [startup-seeker](https://startup-seeker.com/company/kapso~com) · [360dialog: coexistencia](https://docs.360dialog.com/partner/waba-management/phone-number-and-hosting/using-whatsapp-app-and-cloud-api-simultaneously) · [360dialog: coexistence webhooks](https://docs.360dialog.com/docs/waba-basics/phone-numbers/coexistence/coexistence-webhooks) · [Telnyx: coexistence webhooks](https://developers.telnyx.com/docs/messaging/whatsapp/coexistence/webhooks) · [seven.io](https://help.seven.io/en/whatsapp/whatsapp-coexistence) · [Chakra HQ](https://chakrahq.com/article/whatsapp-coexistence-live-eu-uk-europe-whatsapp-business-for-api-live/) · [comunidad Make: echoes](https://community.make.com/t/whatsapp-coexistence-watch-events-does-not-expose-smb-message-echoes/114713)
- Código: rutas y líneas citadas en la §1.3 (HEAD `b594cc4`). GCP: `gcloud run services describe`, `get-iam-policy`, `tasks queues list`, `secrets list`, `logging read` (2026-10-12).
