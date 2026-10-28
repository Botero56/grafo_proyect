# j2ys-chat-automatization

Backend en **FastAPI** que atiende WhatsApp para múltiples negocios
(multi-tenant) de distintas verticales (hoy restaurantes, a futuro
odontología), usando agentes de **Google ADK** respaldados por **Firestore**
(vía el ODM `fireo`).

Este README documenta el estado del proyecto después de la sesión en la que
se construyó el flujo real de webhook + API de administración. Está escrito
para leerse con calma: explica la arquitectura completa, no solo lo nuevo.

---

## 1. Idea general

1. Un negocio (restaurante, consultorio dental, ...) tiene una línea de
   WhatsApp Business conectada a este backend.
2. Un cliente le escribe → WhatsApp manda un webhook → el backend identifica
   **a qué negocio** le escribieron y **quién** escribe.
3. El backend decide el **rol** de quien escribe (`admin` o `cliente`) —
   **la IA nunca decide esto**, es una regla de seguridad explícita del
   proyecto.
4. Según el rol y la vertical del negocio, se invoca el **agente de IA**
   correspondiente (ADK), se genera una respuesta, se envía por WhatsApp y
   se guarda todo en Firestore.
5. Aparte del webhook, hay una **API de administración** (protegida con API
   key) para dar de alta negocios, configurarlos y cargar su menú — el
   mecanismo para escalar de 2 restaurantes a muchos más sin tocar código.

---

## 2. Estructura de carpetas

```
Dockerfile.webhook              # imagen del servicio público (webhook + onboarding)
Dockerfile.worker               # imagen del worker privado (tareas + API admin)
docker-compose.yml              # entorno local: webhook + worker + emulador de Cloud Tasks
cloudbuild.yaml                 # build de las 2 imágenes + deploy a Cloud Run

app/
  config.py                     # settings (pydantic), leídas de .env

  firestore_db/
    firebase_schemas.py         # TODOS los modelos de Firestore (fireo)

  schemas/
    enum_schemas.py             # enums compartidos (status, roles, etc.)
    api/                        # Pydantic request/response de la API HTTP
      business_schemas.py
      configuration_schemas.py
      menu_item_schemas.py

  services/
    firestore_services/
      firestore_service.py          # FireStoreCRUDService: create/get/update/delete genérico
      firestore_webhook_service.py  # WhatsAppWebhookService: parsea el payload de WhatsApp
      tenant_scoped_service.py      # TenantScopedQuery: acceso a Firestore con scoping por business_id
    whatsapp_client/
      whatsapp_client.py            # WhatsAppClient: envía mensajes vía Graph API
    messaging/
      inbound_pipeline.py           # ingesta + flush de un mensaje entrante (lo usa el worker)
      outbound_message_service.py   # OutboundMessageService: envía + persiste el mensaje saliente
      webhook_payload.py            # split de eventos del payload (puro, lo usa el webhook)
    webhook_entry.py                # SERVICIO 1 (público): GET/POST /webhook, /onboarding
    worker_entry.py                 # SERVICIO 2 (privado): /tasks/* + /api/v1/admin/*
    printer_client/                 # cliente para impresora térmica — aún no conectado al flujo

  ai/
    core/
      agent_runner.py           # AgentRunnerService: ejecuta un Agent ya construido y devuelve texto
    vertical_registry.py        # mapea Businesses.bussiness_vertical -> (get_agent, resolve_role)
    ai_restaurant/
      __init__.py                # admin_agent, customer_agent, get_agent(role)
      role_resolver.py           # resolve_user_role(): admin vs cliente, backend-only
      admin/
        agent.py                 # admin_agent = Agent(...) de google-adk
        admin_tools.py           # tools del admin (stubs, NotImplementedError)
        prompt/restaurant_admin_agent_prompt.py
      user/
        agent.py                 # customer_agent = Agent(...) de google-adk
        tools/customer_tools.py  # tools del cliente (stubs, NotImplementedError)
        prompt/restaurant_user_agent_prompt.py
    (ai_odontologia/ no existe todavía — ver sección 8)

  api/
    v1/
      deps.py                    # require_api_key: dependencia de auth para endpoints admin
      router.py                  # agrega los sub-routers admin (montados en el worker)
      admin/
        businesses_router.py     # crear/leer negocios
        configurations_router.py # leer/editar configuración + admin_phone_numbers
        restaurant/
          menu_items_router.py   # CRUD de MenuItems (vertical-específico)
```

---

## 3. Modelo de datos (Firestore, `app/firestore_db/firebase_schemas.py`)

Todo cuelga de **`business_id`** (multi-tenant desde el día uno). Las
colecciones marcadas "restaurante" son específicas de esa vertical; el
resto es genérico y lo reutilizaría odontología.

| Colección | Para qué sirve | Campos clave |
|---|---|---|
| `businesses` | El tenant. Un doc = un negocio con su línea de WhatsApp. | `bussiness_id` (ID), `bussiness_vertical` (`"restaurant"` / `"dentist"`), `whatsapp_phone_number`, **`whatsapp_phone_number_id`** *(nuevo)*, `status` |
| `configurations` | Configuración por negocio (1:1 con `businesses`). | `business_id`, **`admin_phone_numbers`** *(nuevo, lista)*, `manager_phone`, flags de impresora/confirmación |
| `customers` | Clientes de un negocio. | `business_id`, `phone_number`, `whatsapp_user_id` |
| `conversations` | Una conversación activa cliente↔negocio. | `business_id`, `customer_id`, `status`, `last_message_id` |
| `messages` | Cada mensaje, entrante o saliente. | `conversation_id`, `role` (`user`/`assistant`), `content`, `status` |
| `menu_items` | *(restaurante)* Ítems del menú. | `business_id`, `name`, `price`, `is_active` |
| `orders`, `status_history` | *(restaurante)* Pedidos y su historial. | sin cambios en esta sesión |
| `webhook_events` | Auditoría cruda de cada webhook recibido, **ahora idempotente** (ver sección 5). | `webhook_event_id` = ID del documento = `wa_message_id` |
| `print_jobs` | *(restaurante)* Cola de impresión térmica. | sin cambios en esta sesión |

### Campos nuevos agregados en esta sesión

- `Businesses.whatsapp_phone_number_id`: el `phone_number_id` de la Graph
  API de WhatsApp (distinto del número visible). Necesario para poder
  **enviar** mensajes por negocio, no solo recibirlos.
- `Configurations.admin_phone_numbers` (lista): los teléfonos que el
  backend reconoce como administradores de ese negocio. Es la pieza central
  de la resolución de rol (sección 5).

Ambos son campos aditivos: los documentos existentes se leen con valor
vacío/`None`, sin necesidad de migración.

---

## 4. La arquitectura de IA (por qué es reutilizable entre verticales)

Esto se construyó en la sesión anterior a esta y se terminó de conectar en
esta. La idea: **el agente en sí no cambia entre restaurante y odontología,
solo su configuración (tools, prompt, modelo)**.

Cada rol de cada vertical es un `google.adk.agents.Agent(name, model,
description, instruction, tools)` construido directamente con sus propias
tools/prompt (ver `ai_restaurant/admin/agent.py` y `ai_restaurant/user/agent.py`).
Antes pasaba por un wrapper `agent_factory.py` (`AgentConfig` +
`build_agent`) que solo reenviaba esos cinco campos; se eliminó el
2026-09-30.

Cada vertical expone exactamente dos cosas hacia afuera:

```python
# app/ai/ai_restaurant/__init__.py
get_agent(role: str) -> Agent          # "admin" | "cliente"

# app/ai/ai_restaurant/role_resolver.py
resolve_user_role(business_id, phone_number, customer_id) -> {"role": ..., "customer_id": ...}
```

`app/ai/vertical_registry.py` mapea `Businesses.bussiness_vertical` a esas
dos funciones de la vertical correspondiente:

```python
get_vertical_handlers("restaurant") -> VerticalHandlers(get_agent=..., resolve_role=...)
```

**Agregar odontología en el futuro es**: escribir `app/ai/ai_odontologia/`
con la misma forma (sus `Agent` + get_agent + role_resolver propios) y
agregar una entrada en `_VERTICALS`. **Nada del webhook cambia.**

### Pieza que faltaba y se agregó esta sesión: ejecutar el agente

Tener un `Agent` de ADK construido no alcanza para responder un mensaje —
falta correrlo. `app/ai/core/agent_runner.py` (`AgentRunnerService`) hace
eso: mantiene sesiones en memoria (una por `(business_id, conversation_id)`)
y usa `google.adk.runners.Runner` para convertir "agente + texto entrante"
en "texto de respuesta". Si algo falla acá (el agente truena, un tool no
implementado explota), nunca se propaga el error — se loguea y se devuelve
un mensaje de fallback, porque el webhook **siempre** debe responder 200.

**Limitación conocida**: las sesiones son en memoria, por proceso — no
sobreviven un restart ni escalan a múltiples workers. Aceptable para el
volumen actual, a revisar si crece el tráfico.

---

## 5. Cómo se resuelve el rol (admin vs. cliente) — el punto de seguridad

Antes de esta sesión, esto no existía: no había forma de que el backend
supiera quién es admin de un negocio. La decisión tomada fue:

> Los teléfonos autorizados como admin de un negocio viven en
> `Configurations.admin_phone_numbers` (una lista). El backend compara el
> teléfono de quien escribe contra esa lista **antes** de invocar cualquier
> agente. La IA jamás participa en esta decisión.

```python
# app/ai/ai_restaurant/role_resolver.py
def resolve_user_role(business_id, phone_number, customer_id=None) -> dict:
    configuration = Configurations.collection.filter(business_id=business_id).get()
    if phone_number in (configuration.admin_phone_numbers or []):
        return {"role": "admin", "customer_id": None}
    return {"role": "cliente", "customer_id": customer_id}
```

Si un negocio no tiene admins configurados todavía, `admin_phone_numbers`
está vacía por defecto → **todo el mundo resuelve a "cliente"** (seguro por
defecto, nadie es admin "por accidente").

Para cargar admins de un negocio se usa el endpoint:
`PUT /api/v1/admin/configurations/{business_id}/admin-phone-numbers`.

**Nota pendiente, no resuelta en esta sesión**: no hay normalización de
formato de teléfono (E.164 vs. como llega de WhatsApp vs. como se guarda en
`Customers`). Hoy se compara el string tal cual. Si en el futuro hay
inconsistencias de formato, la comparación de rol puede fallar
silenciosamente — vale la pena revisarlo antes de tener muchos negocios.

---

## 6. El flujo completo del webhook

Son **dos servicios**, cada uno con su imagen (ver sección 9):

- **webhook** (`app/services/webhook_entry.py`, público):
  **`GET /webhook`** hace el handshake de Meta (`hub.verify_token` contra
  `VERIFY_TOKEN`). **`POST /webhook`** verifica la firma (Meta o Kapso),
  separa los eventos del payload y encola cada uno en Cloud Tasks. No toca
  Firestore ni la IA, y responde en milisegundos.
- **worker** (`app/services/worker_entry.py`, privado): Cloud Tasks le
  entrega cada evento en `/tasks/process-event` (ingesta) y, unos segundos
  después, `/tasks/flush-conversation` contesta todo lo que el cliente
  escribió en esa ventana. La lógica vive en
  `app/services/messaging/inbound_pipeline.py`.

Lo que hace el worker con un mensaje, paso a paso:

1. `WhatsAppWebhookService().process_incoming_payload(body)`:
   - **Idempotencia** *(nuevo)*: si `webhook_event_id` (= el `wa_message_id`
     de WhatsApp) ya existe en Firestore, corta acá y devuelve
     `{"duplicate": True, ...}` — nada se vuelve a crear. Esto es importante
     porque **WhatsApp reintenta la entrega del mismo mensaje varias
     veces**; sin esto, cada reintento creaba un cliente/conversación/
     mensaje duplicado.
   - Si no es duplicado: identifica el `Business` por
     `whatsapp_phone_number`, hace get-or-create de `Customer` y de
     `Conversation`, guarda el `Message` entrante.
2. Si el resultado es duplicado, un status-update (delivered/read), o hubo
   error de parseo → responder `200` de inmediato, sin hacer nada más.
3. `get_vertical_handlers(business.bussiness_vertical)` → obtiene las
   funciones de esa vertical.
4. `resolve_role(business_id, phone_number, customer_id)` → `"admin"` o
   `"cliente"` (sección 5).
5. `get_agent(role)` → el `Agent` de ADK correspondiente.
6. `AgentRunnerService.get_reply(...)` → texto de respuesta.
7. `OutboundMessageService.send_and_persist(...)` → envía la respuesta por
   WhatsApp (`WhatsAppClient`) y guarda el `Message` saliente (`role:
   assistant`), actualizando la conversación.
8. El worker responde `200` salvo fallos ANTES de invocar a la IA
   (`RetryableTaskError` → `500`, Cloud Tasks reintenta): reintentar un
   turno que ya mandó la respuesta se la duplicaría al cliente.

Antes del paso 4 sería el lugar natural para cortar el flujo si hubiera
una confirmación de pedido pendiente (ej. "sí"/"no" a un pedido). Existía un
`PendingConfirmationStore` en memoria pensado para eso, pero nunca se
conectó y se eliminó el 2026-09-30; si se retoma, en Cloud Run debería vivir
en Firestore, no en memoria del proceso.

---

## 7. API de administración (onboarding, no expuesta a WhatsApp)

Todo bajo `/api/v1/admin/...`, servido por el **worker** (privado: Cloud
Run exige IAM antes de llegar acá) y protegido además con el header `X-API-Key`
(comparado contra `AdminAPISettings.API_KEY` en `app/config.py`). Sin esta
key, todo responde `401`.

| Método | Ruta | Qué hace |
|---|---|---|
| `POST` | `/admin/businesses` | Crea un `Business` **y** su `Configurations` por defecto en la misma llamada |
| `GET` | `/admin/businesses/{business_id}` | Lee un negocio |
| `GET` | `/admin/configurations/{business_id}` | Lee la configuración de un negocio |
| `PATCH` | `/admin/configurations/{business_id}` | Actualiza campos generales (parcial) |
| `PUT` | `/admin/configurations/{business_id}/admin-phone-numbers` | Reemplaza la lista completa de admins |
| `POST` | `/admin/restaurant/menu-items` | Crea un ítem de menú *(vertical-específico)* |
| `GET` | `/admin/restaurant/menu-items/{business_id}` | Lista el menú de un negocio |
| `PATCH` | `/admin/restaurant/menu-items/{item_id}?business_id=...` | Edita un ítem (valida que sea del negocio indicado) |
| `DELETE` | `/admin/restaurant/menu-items/{item_id}?business_id=...` | Borra un ítem |

Los endpoints de menú están deliberadamente bajo `admin/restaurant/` (en la
ruta y en la carpeta) para que sea obvio que son de esa vertical y no
genéricos — cuando exista odontología, sus endpoints propios (ej. de
servicios/citas) irían en `admin/odontologia/`, sin tocar
`businesses_router.py` ni `configurations_router.py`.

### Por qué existe `TenantScopedQuery`

`app/services/firestore_services/tenant_scoped_service.py` centraliza el
patrón "filtrar por `business_id`" para que ningún endpoint nuevo pueda
olvidarlo. En particular, `get_by_id_for_business(model, business_id, id)`
trae el documento por ID y **valida que su `business_id` coincida** antes de
devolverlo — así, aunque alguien adivine o fuerce un ID de otro negocio, no
puede leer/editar datos ajenos por ese camino.

---

## 8. Variables de entorno (`.env`)

```
PROJECT_ID=...          # proyecto de GCP/Firestore
DATABASE=...            # nombre de la base de Firestore
TEMP_TOKEN_WPP=...      # token de la API de WhatsApp Cloud
VERIFY_TOKEN=...        # token de verificación del webhook (handshake con Meta)
MODEL_RESTAURANT=...    # modelo usado por los agentes de ai_restaurant
PHONE_NUMBER_ID=...     # (nuevo) phone_number_id de Graph API, fallback si un negocio no tiene el suyo propio
API_KEY=...             # (nuevo) API key para los endpoints /admin/*
```

`PHONE_NUMBER_ID` y `API_KEY` se agregaron en esta sesión con valor
`CHANGE_ME` — **hay que reemplazarlos con valores reales** antes de correr
el proyecto en serio.

---

## 9. Cómo correr y probar

**Entorno local completo** (los dos servicios con sus imágenes reales y el
emulador de Cloud Tasks en medio):

```bash
# una vez: credenciales para Firestore/Vertex AI reales
gcloud auth application-default login
# en .env: GOOGLE_ADC_FILE=<ruta a application_default_credentials.json>

docker compose up --build
# webhook -> http://localhost:8080   worker -> http://localhost:8081
```

**Tests** (cubren los dos servicios; `uv sync` instala todos los grupos):

```bash
uv run pytest
```

**Prueba de idempotencia**: la cubren `tests/test_webhook_event_router.py`
y `tests/test_e2e_flow.py` contra un Firestore simulado (`uv run pytest`).
El antiguo `scripts/test_webhook_processing.py` escribía en Firestore real y
se eliminó (2026-09-30): `pytest` a secas lo recogía por su nombre.

**Prueba manual** (con `docker compose up` corriendo):

```bash
curl "http://localhost:8080/webhook?hub.mode=subscribe&hub.verify_token=<VERIFY_TOKEN>&hub.challenge=123"
# → 200, body "123"

curl -X POST http://localhost:8081/api/v1/admin/businesses   -H "X-API-Key: <API_KEY>" -H "Content-Type: application/json"   -d '{"name":"...","legal_name":"...","bussiness_vertical":"restaurant","phone":"...","whatsapp_phone_number":"..."}'
```

Un `POST /webhook` necesita la firma `X-Hub-Signature-256` (HMAC del body
con `APP_SECRET`); sin ella responde `401`.

### Despliegue (Cloud Run)

```bash
gcloud builds submit --config cloudbuild.yaml --region us-central1
```

Construye las dos imágenes, las sube a Artifact Registry y despliega
primero el worker (`j2ys-lt-app`, privado, `--max-instances=1`) y después
el webhook (`j2ys-lt-plain`, público). Conserva las variables de entorno,
la cuenta de servicio y el IAM (quién puede invocar) ya configurados en
cada servicio.

Requisitos, una sola vez:

```bash
PROJECT=j2ys-tech-chat-automation
REGION=us-central1
gcloud artifacts repositories create j2ys --repository-format=docker --location $REGION
# Cloud Build corre con la cuenta de Compute por defecto
# (435766564352-compute@developer.gserviceaccount.com, roles/editor): le
# alcanza para subir imágenes y desplegar. No se le dan más permisos.
# Variable que ya no se usa (antes elegía el rol con una sola imagen):
gcloud run services update j2ys-lt-app --region $REGION --remove-env-vars SERVICE_ROLE
gcloud run services update j2ys-lt-plain --region $REGION --remove-env-vars SERVICE_ROLE
```

Para usar la API admin en producción: `gcloud run services proxy
j2ys-lt-app --region us-central1` y llamar a `http://localhost:8080`. Cloud
Scheduler invoca `/api/v1/admin/jobs/*` del worker con un token OIDC y la
cabecera `X-API-Key`.

**Nota importante**: `admin_tools.py` y `customer_tools.py` siguen siendo
stubs (`raise NotImplementedError`). Un mensaje real que llegue por
WhatsApp va a hacer que el agente intente llamar una tool y falle — el
`AgentRunnerService` atrapa eso y responde con un mensaje de fallback
genérico. Eso es esperado: el objetivo de esta sesión era dejar el **tubo**
completo funcionando (webhook → rol → agente → respuesta → WhatsApp →
Firestore), no implementar la lógica de negocio de las tools todavía.

---

## 10. Bugs pre-existentes que se encontraron y corrigieron

Ninguno de estos estaba en el plan original — aparecieron al conectar
piezas que nunca se habían ejecutado juntas de punta a punta, y bloqueaban
directamente lo que se estaba construyendo:

1. **`FireStoreCRUDService` no podía conectar a Firestore.** Hacía
   `firestore.Client(**credentials)` sobre un objeto pydantic (no un dict)
   con nombres de campo (`PROJECT_ID`, `DATABASE`) que no coinciden con lo
   que espera `firestore.Client` (`project`, `database`). Se corrigió para
   usar el mismo patrón que ya funcionaba en `firestore_webhook_service.py`.
2. **`DateTime(auto_now=True)` no existe en `fireo`.** El parámetro
   correcto es `auto_update`. Esto rompía `.save()`/`.to_dict()` en
   `MenuItems` y `Configurations` — cualquier intento de guardar o leer uno
   de esos documentos tronaba con `UnSupportedAttribute`.
3. **`RestaurantAISettings`/`DentistAISettings` no tenían `model_config`**,
   a diferencia de las otras clases de settings — funcionaba "de casualidad"
   porque el comportamiento por defecto de pydantic-settings también lee
   `.env`, pero quedaba inconsistente con el resto. Se alineó.

`main.py` también tenía la app real de FastAPI comentada dentro de un
docstring desde el commit anterior — nunca se estaba sirviendo nada, solo
corría un script de prueba manual al final del archivo. Eso se separó a
`scripts/test_webhook_processing.py` y `main.py` quedó como la app real.
(2026-10-13: `main.py` y su variable `SERVICE_ROLE` se eliminaron; cada
servicio arranca su propio entrypoint desde su Dockerfile.)

---

## 10.1. Handoff bot ↔ humano (Coexistencia)

Ver [`HANDOFF.md`](./HANDOFF.md): traspaso entre el bot de IA y un humano
cuando el negocio usa la app de WhatsApp Business en el móvil a la vez que
esta API (mensajes de cliente, status, ecos de `smb_message_echoes`,
pausa/reactivación del bot).

---

## 11. Qué queda fuera de esta sesión (a propósito)

- Implementar la lógica real de `admin_tools.py` / `customer_tools.py`
  (siguen siendo stubs de Firestore).
- Construir `app/ai/ai_odontologia/` — el punto de extensión
  (`vertical_registry`) ya está listo para recibirlo.
- Confirmación de pedidos sí/no en el flujo del webhook.
- Normalización de formato de teléfono para la comparación de rol admin.
- Soporte multi-WABA real (hoy todos los negocios comparten un solo token
  de WhatsApp, `TEMP_TOKEN_WPP`; solo el `phone_number_id` es por negocio).
