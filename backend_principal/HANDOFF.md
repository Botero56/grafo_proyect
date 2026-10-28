# Handoff bot ↔ humano (Coexistencia WhatsApp)

Este documento explica el traspaso entre el bot de IA y un humano cuando el
negocio usa la app de **WhatsApp Business** en el móvil **a la vez** que
esta API (modo Coexistencia de Meta). Todo llega al mismo endpoint:
`POST /webhook` (servicio webhook, `app/services/webhook_entry.py`).

## 1. Un solo endpoint, cuatro tipos de evento

`WhatsAppWebhookEventRouter` (`app/services/firestore_services/webhook_event_router.py`)
lee `entry[0].changes[0].field` para clasificar cada payload:

| `field` | Qué es | Pasa por la IA |
|---|---|---|
| `messages` + `value.messages` | Mensaje del cliente final | Sí (si el bot está activo) |
| `messages` + `value.statuses` | sent/delivered/read/failed de un mensaje saliente | No — solo actualiza `Messages.status` por `wamid` |
| `smb_message_echoes` | Un humano escribió desde la app móvil | **Nunca** — pausa el bot |
| `history` / `smb_app_state_sync` | Historial / contactos al conectar el número | No — se importan como mensajes `origin=historial` y clientes (cola aparte, ver `ONBOARDING_COEXISTENCIA.md`) |

El caso `messages` (cliente o status) se delega a
`WhatsAppWebhookService.process_incoming_payload` (ya existía, ahora también
guarda auditoría de los status y marca `ultimo_mensaje_cliente_at`). Echo,
history y app_state_sync son manejo nuevo.

## 2. Idempotencia y auditoría

Todo evento se guarda crudo en `WebhookEvents` **antes** de procesarse, con
un `event_kind`. La clave de idempotencia varía por tipo:

- Mensaje de cliente / echo: el `wamid` del mensaje.
- Status: `status:{wamid}:{status}` (un mismo wamid puede tener varios
  status distintos, y cada uno se procesa una sola vez).
- History / app_state_sync: hash del contenido (no traen un id único).

## 3. El flujo de un mensaje de cliente

1. Se guarda el mensaje (`Messages`, `direction=entrante`, `origin=cliente`).
2. Si `Conversations.bot_activo == false` → **no se llama a la IA**, se
   notifica al humano (WhatsApp a `Configurations.manager_phone`) y se corta.
3. Si `bot_activo == true` → se resuelve el rol (admin/cliente, sin que la
   IA participe) y se invoca el agente correspondiente.
4. El agente de cliente (`customer_agent`) puede llamar a la tool
   `request_human_handoff(motivo, confianza)`
   (`app/ai/ai_restaurant/user/tools/customer_tools.py`) SOLO cuando:
   - el cliente pide explícitamente un humano (`solicitud_cliente`), o
   - falta un precio/dato o es variable y la IA no puede inventarlo
     (`precio_variable` / `sin_datos`).
   Cualquier otro motivo (antes existía `baja_confianza`) lo rechaza la
   tool y el router: la IA sigue atendiendo y no se avisa al admin.
5. Si se pidió handoff: se ignora el texto libre del modelo y se manda el
   mensaje de traspaso configurado (`Configurations.handoff_transfer_message`,
   o el default global), y se notifica al humano. **El bot NO se pausa acá**
   -- sigue atendiendo al cliente con normalidad en los siguientes mensajes;
   pausarlo de una vez dejaba al cliente sin respuesta de nadie hasta que un
   humano reactivara a mano (bug real, corregido 2026-09-22). La única forma
   de pausar la conversación es un echo real de la encargada (sección 4).
6. Si no: se envía la respuesta normal y se guarda el `wamid` que devuelve
   la Cloud API.

## 4. El flujo de un echo (humano responde desde el móvil)

1. Se identifica el negocio por `from` (número del negocio) y el cliente
   por `to` (número del cliente) — si el cliente no existía, se crea (un
   humano puede escribirle primero a alguien nuevo).
2. Se guarda el mensaje (`direction=saliente`, `origin=humano`). **No** se
   toca `ultimo_mensaje_cliente_at` — un echo no abre ni extiende la
   ventana de 24h de WhatsApp.
3. Se pausa el bot (`motivo_handoff=respuesta_manual`), fechando la pausa
   con el `timestamp` del echo (Cloud Tasks no garantiza orden). Un echo
   anterior a la última reactivación (`bot_reactivado_at`) no pausa, y un
   flush que ya estaba en cola no contesta si la conversación se pausó.
4. El texto se agrega a la sesión del agente (ADK) como nota de sistema,
   para que cuando el bot se reactive no contradiga ni repita lo que ya
   dijo el humano.

## 5. Reactivación del bot

Todas pasan por `HandoffService.reactivate_bot`:

- **Automática por inactividad de la encargada (mecanismo principal)**: no
  hace falta que nadie la autorice. Cuando el cliente vuelve a escribir con
  el bot pausado, `HandoffService.should_auto_reactivate` chequea si ya
  pasaron `HANDOFF_QUICK_REACTIVATION_SECONDS` (default 30s) desde el
  último mensaje de la encargada (`ultimo_mensaje_humano_at`, o
  `pausado_desde` si nunca hubo un echo). Si sí, ESE MISMO mensaje del
  cliente reactiva la conversación y sigue el flujo normal de la IA (ver
  `app/services/messaging/inbound_pipeline.py::_process_customer_message`). Es
  event-driven -- se chequea al llegar el mensaje, no por un job
  programado, porque 30s es demasiado seguido para Cloud Scheduler y nada
  depende de `bot_activo` hasta que hay un mensaje que responder.
- **Manual (opcional, para forzarlo antes de los 30s)**: desde el chat del
  admin, la tool `reactivate_customer_chat` (`app/ai/ai_restaurant/admin/
  admin_tools.py`), o por API: `POST /api/v1/admin/conversations/{id}/
  reactivate-bot` / `.../close` (cierra la conversación Y reactiva el bot;
  la próxima vez que el cliente escriba se abre una conversación nueva).
- **Por `pausado_hasta` vencido**: respaldo de horizonte largo, barrido
  programado `POST /api/v1/admin/jobs/reactivate-bots` (misma API key
  admin), pensado para **Cloud Scheduler** cada varios minutos -- cubre
  casos raros donde el mecanismo de 30s no aplicó (ej. el cliente nunca
  volvió a escribir).

## 6. Configuración necesaria

**En la app de Meta** (WhatsApp → Configuration → Webhook), suscribir los
campos: `messages`, `smb_message_echoes`, `history` y `smb_app_state_sync`
(los cuatro obligatorios para números conectados en coexistencia).

**Variables de entorno nuevas** (`.env`):

```
APP_SECRET=...                 # App Secret de Meta (NO el token de acceso), valida X-Hub-Signature-256
HANDOFF_INACTIVITY_MINUTES=240           # opcional, default 240
HANDOFF_DEFAULT_TRANSFER_MESSAGE="..."   # opcional, default en app/config.py
```

**URL del webhook**: `<URL del servicio webhook>/webhook`. En desarrollo
local eso es la URL de tu túnel (Cloudflare Tunnel u otro) apuntando a
`docker compose` (puerto 8080); en producción, la URL pública de Cloud Run.

## 7. Campos nuevos en Firestore (aditivos, sin migración real)

Firestore/`fireo` no versiona esquema: los documentos existentes se leen
con estos campos en `None`/default, sin backfill necesario.

- `Conversations`: `bot_activo`, `motivo_handoff`, `agente_asignado`,
  `pausado_desde`, `pausado_hasta`, `ultimo_mensaje_cliente_at`,
  `ultimo_mensaje_humano_at`.
- `Messages`: `direction` (entrante/saliente), `origin` (cliente/ia/humano).
- `Configurations`: `handoff_transfer_message`.
- `WebhookEvents`: `event_kind`.

## 8. Tests

`uv run pytest tests/` — cubre firma inválida/válida, los tres tipos de
payload (mensaje, status, echo), idempotencia, echo en conversación
inexistente, mensaje de cliente con el bot pausado, y el barrido de
reactivación. Firestore se simula con `tests/fakes.py` (no pega a Firestore
real ni requiere credenciales).
