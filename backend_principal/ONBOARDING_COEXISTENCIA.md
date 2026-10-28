# Onboarding por WhatsApp Embedded Signup (coexistencia)

Estado: **prueba con número propio** (2026-10-09). Todo se expone con la URL
por defecto de Cloud Run. Lo que falta antes del primer cliente está en la
sección 7 y marcado en el código como `TODO(seguridad)`.

- App de Meta: `restaurante-edy-automatizacion` (App ID `2103790087686306`),
  portafolio Edy-gourmet-automatizacion (`1666940495142299`).
- **Embedded Signup v4** (Meta da de baja v2 el 2026-10-15): `extras` vacío;
  la versión y los productos salen de la configuración de Facebook Login for
  Business (`META_CONFIG_ID`). Si el flujo no ofrece "conectar la app de
  WhatsApp Business existente", activar `META_ES_COEXISTENCE_FEATURE_TYPE=true`
  (manda `extras.featureType = "whatsapp_business_app_onboarding"`).
- Requisito de Meta para coexistencia con terceros: ser Solution Partner o
  Tech Provider. Para la prueba con un número de tu propio portafolio basta
  con tener rol en la app.

- Servicio webhook: `j2ys-lt-plain` → `https://j2ys-lt-plain-435766564352.us-central1.run.app`
- Servicio worker (backend): `j2ys-lt-app` (privado, solo Cloud Tasks con OIDC)
- Proyecto `j2ys-tech-chat-automation`, región `us-central1`, Firestore `j2ys-tech-core`
- Mismo repo, una imagen por servicio (`Dockerfile.webhook` / `Dockerfile.worker`); se despliegan juntos con `cloudbuild.yaml`.

## 1. Flujo

```
Página GET /onboarding?business_id=<nuestro id>       (j2ys-lt-plain)
  └─ FB.login (config_id, response_type=code, extras={} en v4)
       ├─ callback: authResponse.code   (sin code → evento LOGIN_CANCELLED)
       └─ window.message (solo *.facebook.com), type WA_EMBEDDED_SIGNUP:
            FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING: waba_id, phone_number_id (puede faltar)
            CANCEL (current_step) / ERROR (error_code, error_message, session_id)
            FINISH / FINISH_ONLY_WABA → no es coexistencia, se rechaza
  └─ POST /api/whatsapp/onboarding/event                (j2ys-lt-plain) → log "[sesión ES] …"
  └─ POST /api/whatsapp/onboarding                      (j2ys-lt-plain, SÍNCRONO, log "[onboarding n/7] …")
       0. Businesses[business_id] existe y está activo; phone_number_id no es de otro negocio
       1. code → token (GET /oauth/access_token, el code vive ~30 s)
       2. si no vino phone_number_id, se busca en la WABA el número registrado;
          el número pertenece a la WABA y su número visible == Businesses.whatsapp_phone_number
       3. WhatsappCredentials[phone_number_id] (token + estado)
       4. POST /<waba_id>/subscribed_apps
       5. GET /<phone_number_id>?fields=is_on_biz_app,platform_type
          → se espera true / CLOUD_API (informativo: si no coincide, log WARNING
          "Coexistencia NO confirmada" pero el onboarding sigue)
       6. Businesses: whatsapp_phone_number_id, waba_id, coexistence_enabled, ...
       7. Cloud Task → /tasks/onboarding-sync
  └─ Worker /tasks/onboarding-sync                      (j2ys-lt-app, ASÍNCRONO)
       POST /<phone_number_id>/smb_app_data  sync_type=smb_app_state_sync, luego history
  └─ Meta manda webhooks smb_app_state_sync / history → /webhook
       → cola whatsapp-history (lenta) → worker /tasks/process-event
```

**Cómo se ata la conexión a nuestro negocio:** la URL lleva nuestro
`bussiness_id`. Ese id solo no basta, porque puede filtrarse. El endpoint
exige además que el número que Meta dice que se conectó sea el mismo
`Businesses.whatsapp_phone_number` que registramos. El negocio tiene que
crearse antes con su número (`POST /api/v1/admin/businesses`, como hoy).

**Token al enviar:** `WhatsAppClient.for_business` usa el token de
`whatsapp_credentials` si el número está conectado y activo. Si no, usa
`TEMP_TOKEN_WPP`, como hasta ahora. Guarda el token en caché 5 minutos por
proceso.

## 2. Webhooks de coexistencia

| Campo | Qué pasa |
|---|---|
| `messages` | Igual que antes. |
| `smb_message_echoes` | Pausa el bot. La pausa se fecha con el `timestamp` del echo. Un echo más viejo que la última reactivación (`bot_reactivado_at`) no pausa. |
| `history` | Se separa en una tarea por cliente y va a la cola `whatsapp-history`. Se guarda como `Messages` con `origin=historial`, `status=completed` y la fecha original: nunca pasa por la IA, nunca queda pendiente y nunca pausa. Si el dueño no comparte el historial (error 2593109), queda `declined`. |
| `smb_app_state_sync` | Cola `whatsapp-history`. Crea los clientes o les pone nombre. `remove` no borra nada. |

- **Orden:**
  - La reactivación automática compara la hora en que el cliente **escribió** contra el último echo. Un mensaje escrito antes de la respuesta de la encargada nunca reactiva el bot.
  - Un flush que ya estaba en cola no responde si la conversación se pausó mientras esperaba.
- **Idempotencia:**
  - Las tareas tienen nombre determinista: el wamid para mensajes y echoes, y un hash del contenido para history y contactos.
  - Además el worker deduplica en `webhook_events`, y cada mensaje del historial por su wamid.

Meta (documentación revisada el 2026-10-07): sigue vigente `smb_app_data` con
`smb_app_state_sync` y `history`. Hay que pedir los contactos primero. Cada
sincronización se puede pedir una sola vez y hay 24 h desde el onboarding.
No se llama a `/register`. El límite de envío es de 20 mensajes por segundo
por número.

## 3. Variables de entorno

| Nombre | Servicio | Origen | Estado | Para qué |
|---|---|---|---|---|
| `META_APP_ID` | webhook | env Cloud Run | **nueva** (default `2103790087686306`) | SDK JS y `client_id` del intercambio del code |
| `META_CONFIG_ID` | webhook | env Cloud Run | **nueva, obligatoria** | `config_id` de FB.login (configuración v4 de Login for Business) |
| `META_ES_COEXISTENCE_FEATURE_TYPE` | webhook | env Cloud Run | **nueva** (default `false`) | Manda `featureType` en `extras` si v4 no ofrece la coexistencia |
| `CLOUD_TASKS_HISTORY_QUEUE` | webhook | env Cloud Run | **nueva** (default `whatsapp-history`) | Cola de history y contactos |
| `APP_SECRET` | los dos | env Cloud Run | existente | Firma de los webhooks **y** `client_secret` del intercambio. **Debe ser el de la app 2103790087686306.** |
| `GRAPH_API_VERSION` | los dos | env Cloud Run | existente (`v26.0`) | Versión de Graph API (también la usa el SDK JS) |
| `TEMP_TOKEN_WPP`, `PHONE_NUMBER_ID` | los dos | env Cloud Run | existente | Respaldo para negocios sin credencial propia |
| `CLOUD_TASKS_QUEUE`, `WORKER_URL`, `WORKER_SERVICE_ACCOUNT_EMAIL`, `PROJECT_ID`, `DATABASE` | los dos | env Cloud Run | existente | Sin cambios. El webhook ahora también usa `PROJECT_ID`/`DATABASE` en el onboarding. |

Configuradas en Cloud Run que nadie lee: `GRAPH_API_BASE_URL` (`MODEL_DENTIST` se borró el 2026-10-17).

## 4. Despliegue (en este orden)

```bash
PROJECT=j2ys-tech-chat-automation
REGION=us-central1
SA=j2ys-lt-run@$PROJECT.iam.gserviceaccount.com

# 1) Cola de historial: lenta a propósito para no saturar Firestore ni el worker
#    (maxScale=1, concurrencia 40).
gcloud tasks queues create whatsapp-history --location $REGION \
  --max-dispatches-per-second 5 --max-concurrent-dispatches 5 \
  --max-attempts 10 --min-backoff 5s --max-backoff 300s

# 2) Permiso para encolar en la cola nueva (la principal ya lo tiene a nivel de cola).
gcloud tasks queues add-iam-policy-binding whatsapp-history --location $REGION \
  --member serviceAccount:$SA --role roles/cloudtasks.enqueuer

# 3-4) Worker PRIMERO (trae /tasks/onboarding-sync y el manejo de
#      history/contactos), después el webhook (página, endpoint y encolado
#      de history). cloudbuild.yaml ya los despliega en ese orden.
gcloud builds submit --config cloudbuild.yaml --region $REGION
gcloud run services update j2ys-lt-plain --region $REGION \
  --update-env-vars META_APP_ID=2103790087686306,META_CONFIG_ID=1620006579903639,CLOUD_TASKS_HISTORY_QUEUE=whatsapp-history

# 5) Solo si el flujo v4 no ofrece "conectar app de WhatsApp Business existente":
# gcloud run services update j2ys-lt-plain --region $REGION \
#   --update-env-vars META_ES_COEXISTENCE_FEATURE_TYPE=true

# Logs de la prueba (pasos del endpoint y eventos de la página):
gcloud logging read 'resource.labels.service_name="j2ys-lt-plain" AND (jsonPayload.message:"[onboarding" OR jsonPayload.message:"[sesión ES]")' --limit 50 --freshness 1h
gcloud logging read 'resource.labels.service_name="j2ys-lt-app" AND jsonPayload.message:"Sincronización"' --limit 20 --freshness 1h
```

- **Secretos:** no hay ninguno nuevo. `APP_SECRET` ya está como variable de entorno.
- **Reglas de Firestore:** las activas ya niegan todo a los clientes. `firestore.rules` es la copia versionada, así que no hay nada que desplegar.
- **Cola principal:** no se toca. Si quieres reducir los reintentos (hoy son 100), el worker ya los corta en 5: `gcloud tasks queues update whatsapp-messages --location $REGION --max-attempts 10`.

## 5. Configuración manual en Meta (developers.facebook.com → tu app)

1. **Facebook Login for Business → Configuraciones:**
   - Inicio de sesión con OAuth de cliente: **Sí**
   - Inicio de sesión con OAuth web: **Sí**
   - Forzar HTTPS: **Sí**
   - **Iniciar sesión con el SDK de JavaScript: Sí**
   - **Dominios admitidos para el SDK de JavaScript:** `https://j2ys-lt-plain-435766564352.us-central1.run.app`
   - **URI de redireccionamiento de OAuth válidos:** `https://j2ys-lt-plain-435766564352.us-central1.run.app/onboarding`
2. **Facebook Login for Business → Configuraciones (configurations):** una configuración nueva con la variante **WhatsApp Embedded Signup**, los permisos `whatsapp_business_management` y `whatsapp_business_messaging`, y el producto de WhatsApp seleccionado. Seleccionar los productos es lo que la deja en **v4**. Su ID es `META_CONFIG_ID`.
3. **WhatsApp → Configuración → Webhook:**
   - La URL **no cambia:** `https://j2ys-lt-plain-435766564352.us-central1.run.app/webhook`.
   - Suscribir los campos `messages`, `smb_message_echoes`, `history` y `smb_app_state_sync`.
   - Verificar que la app 2103790087686306 sea la que tiene esta URL de callback (hoy sí: tiene 10 campos suscritos).
4. **Modo de la app:** en desarrollo, quien hace el flujo debe tener un rol en la app (admin o desarrollador). Con números de **tu** portafolio no hace falta Tech Provider ni acceso avanzado.

## 6. Checklist de prueba de punta a punta

**Con un número de tu portafolio:**

1. [ ] El negocio existe en `businesses` con `status=active` y `whatsapp_phone_number` = el número en dígitos (ej. `573001234567`). Si no, créalo con `POST /api/v1/admin/businesses`.
2. [ ] El celular tiene WhatsApp Business actualizado (v2.24.17 o superior) con ese número y está a mano.
3. [ ] Abre `https://j2ys-lt-plain-435766564352.us-central1.run.app/onboarding?business_id=<id>`. El botón debe pasar de "Cargando…" a "Conectar WhatsApp".
4. [ ] Haz el flujo y elige "conectar la app de WhatsApp Business existente". Confirma en el celular (código QR o aviso).
5. [ ] La página muestra "¡WhatsApp conectado!".
6. [ ] En Firestore, `whatsapp_credentials/<phone_number_id>` tiene `status=active`, `app_subscribed=true` y las sincronizaciones en `pending` → `requested`.
7. [ ] En Firestore, `businesses/<id>` tiene `waba_id`, `whatsapp_phone_number_id`, `coexistence_enabled=true` y `whatsapp_onboarding_status=connected`.
8. [ ] En los logs del worker aparece "Sincronización smb_app_state_sync pedida a Meta" y luego "history".
9. [ ] Llegan los webhooks `smb_app_state_sync` y `history`: hay clientes nuevos y `messages` con `origin=historial`, y `history_sync_status` pasa a `in_progress` y después a `completed` (o `declined` si no compartiste el historial).
10. [ ] Escribe al número desde otro celular: el bot responde. En los logs, el envío sale con el `phone_number_id` nuevo y sin errores de token.
11. [ ] Responde a mano desde la app del celular: el bot se pausa (`bot_activo=false`, `motivo_handoff=respuesta_manual`).
12. [ ] Pasados más de 30 s sin respuesta de la encargada, escribe de nuevo como cliente: el bot se reactiva.
13. [ ] Repite el paso 3 con un `business_id` de **otro** negocio: la página debe responder "El número que conectaste no coincide…" y no debe guardar nada.
14. [ ] Busca en los logs `EAA` (prefijo de los tokens) y `client_secret`: no debe aparecer nada.

**Con un número de otro portafolio** (cuando aplique): la persona que hace el
flujo debe tener rol en la app mientras esté en modo desarrollo. En modo en
vivo, la app necesita acceso avanzado a los permisos de WhatsApp (Tech
Provider).

## 7. Pendiente para producción

**Aprobado para DESPUÉS de la prueba y ANTES del primer cliente** (no implementado todavía):

1. **Cifrado del token en la aplicación** (decidido: AES-GCM en la app, clave `TOKEN_ENCRYPTION_KEY` en Secret Manager montada en Cloud Run; en Firestore solo el texto cifrado; re-cifrar los tokens existentes; mover también `APP_SECRET` a Secret Manager). Requiere habilitar la API de Secret Manager.
2. **Rate limiting** de `/api/whatsapp/onboarding` y `/api/whatsapp/onboarding/event` (contador en Firestore por IP y por negocio, 429 al pasarse).
3. **Enlace firmado HMAC con vencimiento** en lugar de `?business_id=` (generado desde un endpoint de admin con `API_KEY`).
4. **Subdominio propio con Firebase Hosting**, con rewrite de `/api/whatsapp/**` a `j2ys-lt-plain` (mismo origen, sin CORS); actualizar en Meta los dominios del SDK JS y las URI de redirección.

Detalle de los riesgos:

| Riesgo hoy | Qué hacer |
|---|---|
| Token de Meta en texto plano en Firestore | Moverlo a Secret Manager (un secreto por número; en Firestore solo el nombre) o cifrarlo con Cloud KMS. Hay que habilitar la API de Secret Manager, que hoy está apagada. |
| El onboarding se ata solo por `business_id` + coincidencia del número | Enlace firmado con vencimiento (HMAC) o login real del administrador (Firebase Auth). |
| Sin límite de intentos en `/api/whatsapp/onboarding` | Rate limiting (Cloud Armor con balanceador, o un contador en Firestore). |
| `APP_SECRET`, `TEMP_TOKEN_WPP`, `API_KEY` como variables en texto plano | Secret Manager con `--set-secrets` en Cloud Run. |
| `VERIFY_TOKEN=admin` | Cambiarlo por un valor aleatorio largo y actualizarlo en Meta. |
| URL `*.run.app` | Dominio propio (Firebase Hosting o balanceador) y actualizar los dominios del SDK JS y la URI de redirección en Meta. |
| Servicios `j2ys-lt-*` con `maxScale=1` y etiqueta `loadtest` | Servicios de producción separados, escalado y alertas. |
| `client_secret` viaja en la URL del intercambio (así lo pide Meta) | No activar logging DEBUG de httpx. |
| Caché del token de 5 min por proceso | Si se revoca un token, invalidar la caché (o bajar el TTL). |
| Revocación del token | Manejar el webhook `account_update`/desconexión y marcar la credencial `revoked`. |
| El historial importado no entra al contexto de la IA (las sesiones de ADK no se siembran desde `messages`) | Si se quiere ese contexto, resumir el historial al crear la sesión. |
| La firma `X-Hub-Signature-256` **ya se valida** en `/webhook` | Nada. Verificar que siga así. |
