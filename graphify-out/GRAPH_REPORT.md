# Graph Report - grafo_3b_technology  (2026-10-27)

## Corpus Check
- 246 files · ~187,949 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 3013 nodes · 9290 edges · 153 communities (123 shown, 15 thin omitted)
- Extraction: 89% EXTRACTED · 11% INFERRED · 0% AMBIGUOUS · INFERRED: 1058 edges (avg confidence: 0.93)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Seguimiento de domicilios
- Incidencias de entrega
- Orquestador menú y handoff
- Servicio menú del día
- Pedidos por catálogo WhatsApp
- Enums y pipeline entrante
- API admin de negocios
- Esquemas Firestore
- Extracción de menú por imagen
- Cloud Tasks y auth de tareas
- Heartbeat de dispositivos
- Tools del admin
- Menú activo y stock (tests)
- Router de eventos webhook
- Pool de modelos Gemini
- Proxy del panel (panel_entry)
- Tests API de config del panel
- Flujo de pago del cliente
- Cliente WhatsApp
- Config y servicio Firestore
- Handoff bot/humano
- Horarios y perfil del negocio
- Trabajos de impresión
- API de configuraciones e impresora
- Flush de conversación
- Comprobantes de transferencia
- Estado del pedido
- Proveedor Kapso
- Ruteo de impresora (tests)
- Menú de hoy del admin
- Costo de domicilio
- Tools de pedido
- Webhook entrante y firma
- Domiciliarios
- Privacidad y Kapso
- Esquemas de configuración
- Contrato de impresión Raspberry
- Secret Manager
- Runbook y migración Edy Gourmet
- Sincronización de catálogo
- Jobs de reactivación
- Telemetría Raspberry
- Panel: configuración
- Stock del menú
- Extractores visión y corrección
- Acuerdos de pago y diff de menú
- API Raspberry y túnel (tests)
- Worker y logging
- Formateo de menú
- Memoria de conversación (tests)
- Panel: app y UI
- Sesiones ADK en Firestore
- Reporte Excel dashboard
- Plan de agentes IA
- Agent runner
- Almacenamiento de comprobantes
- Webhooks de coexistencia
- Pedidos catálogo y agente
- Normalización de teléfonos
- Model pool
- Flujo E2E (tests)
- Panel: configService (API)
- Payload del webhook
- Seguridad domicilios y Kapso
- Hardware Raspberry
- Raspberry README y panel
- Panel: validadores de config
- Notas de voz (tests)
- Procesador de audio
- Configuración por cliente y verticales
- Servicio de reporte Excel
- Repositorio Firestore de reportes
- Webhook multi-tenant
- Imagen de menú (tests)
- Cliente WhatsApp (tests)
- Dos tenants (tests)
- API de ítems fijos del menú
- Diff de menú
- Recordatorios de menú
- Zonas y cotizaciones
- Generación de Excel (tests)
- Doc de handoff
- Elección de zona
- Tenants y página de privacidad
- Firma de webhook
- README y despliegue del panel
- Evals de conversación
- Webhook entry (tests)
- Worker entry (tests)
- Panel: validadores
- API de conversaciones
- API de jobs
- Flujos del router (tests)
- Agent runner (tests)
- Memoria de conversación
- Onboarding coexistencia y despliegue
- Planes comerciales
- Privacidad (tests)
- Descarga de medios
- Credenciales Kapso (tests)
- Kill switch
- Handoff humano tool
- Reply guard
- Secretos (tests)
- Evals extra
- Panel: helpers UI
- Módulo test_admin_reactivate_customer_chat
- Logging setup
- Límites de servicios
- Túnel Raspberry
- Panel: config extra
- Panel: páginas HTML
- Módulo __init__
- Módulo test_whatsapp_format
- Módulo firestore_repository
- Módulo set_reminder_hour
- Módulo test_payment_flow
- Módulo landing
- Cliente de impresora
- IAM ops
- Secretos a refs
- Módulo configValidators.test
- Alertas ops
- Módulo ?
- Módulo configuracion
- Módulo orchestrator
- Módulo ?
- Módulo conftest
- Instalación Raspberry
- SSH GitLab Raspberry
- Módulo conftest
- Módulo router
- Módulo __init__
- Deploy
- Deploy del panel
- Backups Firestore
- Módulo pyproject
- Módulo pyproject

## God Nodes (most connected - your core abstractions)
1. `seed_business()` - 276 edges
2. `Businesses` - 141 edges
3. `Orders` - 127 edges
4. `Customers` - 116 edges
5. `seed_configuration()` - 108 edges
6. `WhatsAppClient` - 104 edges
7. `FireStoreCRUDService` - 103 edges
8. `DailyMenuService` - 101 edges
9. `FakeToolContext` - 101 edges
10. `seed_customer()` - 100 edges

## Surprising Connections (you probably didn't know these)
- `Business Wi-Fi from the panel (POST /wifi, nmcli profiles)` --references--> `set_printer_wifi()`  [INFERRED]
  backend_raspberry/README.md → backend_principal/app/api/v1/admin/configurations_router.py
- `Raspberry heartbeat POST /devices/heartbeat (every 60s)` --references--> `device_id()`  [EXTRACTED]
  backend_principal/docs/RUNBOOK.md → backend_raspberry/app/telemetry.py
- `Raspberry heartbeat POST /devices/heartbeat (every 60s)` --references--> `send_heartbeat()`  [EXTRACTED]
  backend_principal/docs/RUNBOOK.md → backend_raspberry/app/telemetry.py
- `Wifi` --shares_data_with--> `set_printer_wifi()`  [INFERRED]
  backend_raspberry/app/main.py → backend_principal/app/api/v1/admin/configurations_router.py
- `Barrido reactivate-bots (Cloud Scheduler)` --implements--> `reactivate_bots()`  [INFERRED]
  backend_principal/HANDOFF.md → backend_principal/app/api/v1/admin/jobs_router.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Flujo de handoff en coexistencia** — backend_principal_handoff_smb_message_echoes, backend_principal_handoff_bot_human_handoff, backend_principal_handoff_auto_reactivation, backend_principal_handoff_reactivation_sweep, backend_principal_app_services_conversation_handoff_service_handoffservice_reactivate_bot [EXTRACTED 1.00]
- **Despliegue webhook + worker en Cloud Run** — backend_principal_cloudbuild_deploy_pipeline, backend_principal_onboarding_coexistencia_webhook_service_j2ys_lt_plain, backend_principal_onboarding_coexistencia_worker_service_j2ys_lt_app, backend_principal_cloudbuild_worker_max_instances_1 [INFERRED 0.85]
- **Seguridad multi-tenant con Kapso** — backend_principal_plan_seguridad_domicilios_secret_manager, backend_principal_plan_seguridad_domicilios_tenant_key, backend_principal_plan_seguridad_domicilios_multitenant_webhook, backend_principal_readme_kapso_hallazgos_kapso [INFERRED 0.85]
- **Raspberry heartbeat -> webhook -> Cloud Tasks -> worker -> devices -> panel status** — backend_principal_docs_runbook_device_heartbeat, backend_principal_readme_webhook_service, backend_principal_readme_cloud_tasks_pipeline, backend_principal_readme_worker_service, backend_principal_docs_runbook_devices_collection, backend_principal_docs_runbook_device_status, panel_central_frontend_configuracion_printer_devices_panel [EXTRACTED 1.00]
- **Comanda printing: panel printer config -> printer token -> Raspberry /print, fallback to admin chat** — panel_central_frontend_configuracion_comandas_section, backend_principal_docs_runbook_printer_config_endpoint, backend_principal_docs_runbook_printer_token, backend_principal_docs_runbook_printer_contract, backend_raspberry_readme_print_endpoint, backend_principal_docs_runbook_admin_chat_fallback [INFERRED 0.85]
- **Inbound WhatsApp message: webhook -> idempotency -> role resolution -> vertical registry -> agent runner -> WhatsApp** — backend_principal_readme_webhook_service, backend_principal_readme_cloud_tasks_pipeline, backend_principal_readme_webhook_idempotency, backend_principal_readme_role_resolution, backend_principal_readme_vertical_registry_pattern, backend_principal_readme_agent_runner, backend_principal_readme_whatsapp_graph_api [EXTRACTED 1.00]
- **Raspberry heartbeat -> webhook -> Cloud Tasks -> worker -> Firestore devices -> panel device list** — backend_raspberry_app_telemetry_start_heartbeat_loop, backend_raspberry_app_telemetry_send_heartbeat, backend_raspberry_app_telemetry_snapshot, contract_post_devices_heartbeat, backend_principal_app_services_webhook_entry_device_heartbeat, backend_principal_app_services_tasks_cloud_tasks_client_enqueue_device_heartbeat, backend_principal_app_services_worker_entry_device_heartbeat_task, backend_principal_app_services_devices_devices_record_heartbeat, backend_principal_app_firestore_db_firebase_schemas_devices, backend_principal_app_services_devices_devices_list_devices, backend_principal_app_api_v1_admin_configurations_router_get_printer_devices, panel_central_frontend_configservice_listprinterdevices, panel_central_frontend_configuracion_loadprinterdevices [EXTRACTED 1.00]
- **Panel printer buttons -> panel proxy -> admin API -> Raspberry /version /update /print /wifi** — panel_central_frontend_configuracion_loadprinterversion, panel_central_frontend_configuracion_updateraspberry, panel_central_frontend_configuracion_testprint, panel_central_frontend_configuracion_savewifi, panel_central_frontend_configservice_request, backend_principal_app_services_panel_entry_forward, backend_principal_app_api_v1_admin_configurations_router_get_printer_device, backend_principal_app_api_v1_admin_configurations_router_update_printer_device, backend_principal_app_api_v1_admin_configurations_router_print_test_page, backend_principal_app_api_v1_admin_configurations_router_set_printer_wifi, backend_principal_app_api_v1_admin_configurations_router_call_raspberry, backend_principal_app_api_v1_admin_configurations_router_printer_target, backend_raspberry_app_main_require_auth, backend_raspberry_app_main_get_version, backend_raspberry_app_main_post_update, backend_raspberry_app_main_post_print, backend_raspberry_app_main_post_wifi [INFERRED 0.95]
- **Panel saves printer URL+token -> Secret Manager + printer_settings -> order ticket printed on Raspberry** — panel_central_frontend_configuracion_savesection, panel_central_frontend_configservice_updatesection, backend_principal_app_api_v1_admin_configurations_router_put_printer, backend_principal_app_services_secrets_init_put_secret, contract_printer_token, backend_principal_app_services_printer_client_print_jobs_printer_for, backend_principal_app_services_printer_client_print_jobs_send_print_job, backend_principal_app_services_printer_client_printer_client_printerclient_print_text, backend_raspberry_app_main_require_auth, backend_raspberry_app_main_post_print [INFERRED 0.85]

## Communities (153 total, 15 thin omitted)

### Community 0 - "Seguimiento de domicilios"
Cohesion: 0.06
Nodes (86): liquidacion_domiciliarios(), Qué entregó cada domiciliario en un día: sus comandas liquidadas, el efectivo y…, get_agent(), Resolución de rol (admin/cliente) del restaurante. Esto NO es una tool del…, Determina si quien escribe es administrador, domiciliario o cliente. Compara el…, resolve_user_role(), DailyOrderCounters, Un documento por negocio y día (id "<business_id>_<YYYY-MM-DD>"): el… (+78 more)

### Community 1 - "Incidencias de entrega"
Cohesion: 0.09
Nodes (68): avisar_tiempo_entrega(), Le avisa a un cliente cuánto falta, más o menos, para que le llegue su pedido…, Registra lo que el cliente decide sobre una novedad en la entrega de su pedido…, responder_incidencia(), Incidents, Problema de un domiciliario con una entrega (no abren, dirección errada...).…, IncidentStatus, Incidents.status. Activas: ESPERANDO_CLIENTE y ESCALADA (una sola por comanda);… (+60 more)

### Community 2 - "Orquestador menú y handoff"
Cohesion: 0.05
Nodes (46): ask_zone_choice(), Crea o completa datos del cliente que falten (dirección, notas, etc.). El…, Le manda al cliente la lista de barrios posibles para que elija el suyo (lo…, upsert_customer_profile(), Businesses, Customers, admin_template(), log_undelivered() (+38 more)

### Community 3 - "Servicio menú del día"
Cohesion: 0.07
Nodes (39): DailyMenus, Menú del día extraído de una imagen que manda un admin autorizado. Colección…, DailyMenuStatus, DailyMenuService, Ciclo de vida de un DailyMenu: crear borrador, descartar el pendiente anterior,…, La foto de una carta de antes de las subidas (carta_media_id sin carta activa).…, El menú de hoy como lo ven los clientes: el menú del día y la carta (productos…, El borrador de carta que se está armando hoy. Uno de otro día se descarta: el… (+31 more)

### Community 4 - "Pedidos por catálogo WhatsApp"
Cohesion: 0.07
Nodes (45): Le dice al sistema qué producto del menú es un código del catálogo de WhatsApp…, resolve_catalog_code(), CatalogMappings, Qué producto del menú es cada código del catálogo de WhatsApp, cuando la…, mapping_id(), pending_codes(), (resueltos, desconocidos, ignorados). Cada uno lleva codigo, cantidad, precio y…, Deja el código como `pending` y anota la conversación que lo espera. True solo… (+37 more)

### Community 5 - "Enums y pipeline entrante"
Cohesion: 0.08
Nodes (54): get_vertical_handlers(), BusinessStatus, BussinessVertical, ConversationStatus, DeliveryEventSource, DeviceEventType, HandoffMotivo, MessageDirection (+46 more)

### Community 6 - "API admin de negocios"
Cohesion: 0.08
Nodes (60): _bot_status_response(), _business_response(), create_business(), _credentials_status(), _ensure_number_is_free(), get_bot_status(), get_business(), _get_business_or_404() (+52 more)

### Community 7 - "Esquemas Firestore"
Cohesion: 0.07
Nodes (44): Configurations, Conversations, Messages, Meta, OrderStatusHistory, Model, db.py Modelos de Firestore (fireo) + inicialización, todo en un solo archivo.…, WebhookEvents (+36 more)

### Community 8 - "Extracción de menú por imagen"
Cohesion: 0.17
Nodes (55): DownloadedMenuImage, MenuIngestionOrchestrator, Acompanamiento, CartaItem, ExtractedUpload, MenuDelDia, OtrasOpciones, BaseModel (+47 more)

### Community 9 - "Cloud Tasks y auth de tareas"
Cohesion: 0.06
Nodes (49): _create_named_task(), enqueue_device_heartbeat(), enqueue_event(), enqueue_flush(), enqueue_incident_timeout(), enqueue_sync_event(), _ensure_queue_in_emulator(), _envelope() (+41 more)

### Community 10 - "Heartbeat de dispositivos"
Cohesion: 0.11
Nodes (45): DeviceEvents, Devices, Estado ACTUAL de una Raspberry (último heartbeat). Un documento por…, Historial de una Raspberry: un documento por CAMBIO detectado entre dos…, DeviceEventResponse, DeviceResponse, HeartbeatPayload, BaseModel (+37 more)

### Community 11 - "Tools del admin"
Cohesion: 0.09
Nodes (39): add_courier(), _business_configuration(), _courier_view(), _find_customer(), find_orders_by_customer(), generate_excel_report(), get_current_datetime(), get_customer_chat_reference() (+31 more)

### Community 12 - "Menú activo y stock (tests)"
Cohesion: 0.12
Nodes (40): Pide confirmar un cambio de stock de HOY. NO lo guarda: le manda al admin un…, request_stock_change(), get_active_menu(), Valida un pedido contra el menú del día vigente AHORA. Productos pedibles:…, Obtiene el menú del día vigente HOY del restaurante: dos productos del día,…, validate_order_items(), match_product(), Los nombres de hoy que corresponden a lo que escribió el admin: exacto,… (+32 more)

### Community 13 - "Router de eventos webhook"
Cohesion: 0.07
Nodes (34): is_business_active(), Kill switch: un negocio que no está "active" no pasa nada por la IA. Sin status…, customer_note(), Nota del sistema para el turno de la IA del cliente (no se guarda)., find_business(), Negocio dueño del número que recibió (o envió) el evento. Con `business_id`…, _digits(), _mark_processed() (+26 more)

### Community 14 - "Pool de modelos Gemini"
Cohesion: 0.08
Nodes (34): APIError, agent_model(), FallbackGemini, Client, Cliente de google.genai para todas las llamadas al modelo: los agentes ADK…, Hace que generate_content (sync y async) recorra MODEL_POOL. En async,…, Gemini de ADK con el cliente del pool (creado al primer uso, como ADK)., El modelo de un agente ADK (por defecto MODEL_RESTAURANT), con fallback al… (+26 more)

### Community 15 - "Proxy del panel (panel_entry)"
Cohesion: 0.07
Nodes (36): api_route, _access_certs(), forward(), home(), _id_token(), NoCacheStaticFiles, get, Request (+28 more)

### Community 16 - "Tests API de config del panel"
Cohesion: 0.11
Nodes (36): seed_business(), seed_configuration(), biz(), fixture, test_panel_pide_api_key(), flow(), fixture, setup() (+28 more)

### Community 17 - "Flujo de pago del cliente"
Cohesion: 0.11
Nodes (37): Registra si se le cobra o no el pedido a un cliente que dijo tener un acuerdo…, resolve_payment_agreement(), Guarda el pedido ya confirmado por el cliente en Firestore. customer_id y…, Guarda cómo va a pagar el cliente un pedido ya guardado. Llámala después de…, Pide a la administración que confirme si este pedido se cobra, cuando el…, request_payment_agreement(), save_order(), set_payment_method() (+29 more)

### Community 18 - "Cliente WhatsApp"
Cohesion: 0.07
Nodes (17): _mask(), Any, Response, Headers de auth para `url`. Con Kapso, la API key solo viaja a su propio host:…, Kapso envuelve algunas respuestas en `{"data": {...}}`; Graph API no. Si `key`…, Tras un 401: relee la key de Secret Manager. True solo si cambió (la rotaron);…, Respuesta de Graph API, o None si el envío falló (ya logueado). Reintenta solo…, Una línea con lo necesario para diagnosticar: código HTTP, código de Meta (190… (+9 more)

### Community 19 - "Config y servicio Firestore"
Cohesion: 0.08
Nodes (28): AdminAPISettings, CloudTasksSettings, DailyMenuSettings, FirestoreSettings, HandoffSettings, PrinterSettings, field_validator, Credenciales para que google-adk/google-genai use Vertex AI en vez de la Gemini… (+20 more)

### Community 20 - "Handoff bot/humano"
Cohesion: 0.11
Nodes (32): HandoffService, El aviso en una sola línea apta para un parámetro de plantilla., template_param(), _notify_outside_window(), Avisos al admin fuera de la ventana de 24h: plantilla por negocio y por tipo de…, test_a_notice_without_its_template_is_an_error_not_a_warning(), test_each_notice_uses_its_own_template(), test_patch_validates_admin_templates() (+24 more)

### Community 21 - "Horarios y perfil del negocio"
Cohesion: 0.11
Nodes (33): _closed_by_admin(), describe_next_opening(), describe_weekly(), evaluate(), _hhmm(), _intervals(), invalidate(), date (+25 more)

### Community 22 - "Trabajos de impresión"
Cohesion: 0.14
Nodes (32): print_order_ticket(), Tools usadas por el customer_agent: menú, perfil del cliente y ciclo de pedido., Envía la comanda del pedido ya guardado a la impresora térmica del negocio.…, PrintJobs, PaymentMethod, PrintJobStatus, build_ticket(), _phone() (+24 more)

### Community 23 - "API de configuraciones e impresora"
Cohesion: 0.12
Nodes (33): _call_raspberry(), get_configuration(), _get_configuration_or_404(), get_printer(), get_printer_device(), get_printer_devices(), get_printer_events(), _normalized_or_400() (+25 more)

### Community 24 - "Flush de conversación"
Cohesion: 0.15
Nodes (33): _claim_pending_messages(), Se lleva los mensajes del cliente que todavía nadie contestó y avanza la marca…, _context(), _incoming(), asyncio, La marca de agua (`Conversations.ultimo_mensaje_respondido_at`) es lo que hace…, Una conversación sin marca de agua (todas las anteriores a este campo) no debe…, Según cómo se escribieron, Firestore puede devolver fechas sin tzinfo;… (+25 more)

### Community 25 - "Comprobantes de transferencia"
Cohesion: 0.17
Nodes (32): TransferReceiptHandler, _overpaid_order(), _pending_transfer_with_held_ticket(), Flujo de pago: el método queda asociado al pedido y viaja a la impresora desde…, Lo que Gemini "leyó" en el comprobante., Pedido de $21.000 con una transferencia de `paid` (de menos)., Casi siempre que transfieren de menos es porque el resto lo pagan en efectivo:…, Pedido de $21.000 pagado con una transferencia de `paid`. (+24 more)

### Community 26 - "Estado del pedido"
Cohesion: 0.16
Nodes (32): confirm_order(), _missing(), Aplica cambios al pedido en curso del cliente. Llámala en el MISMO turno en que…, Confirma el pedido en curso: lo guarda, registra la forma de pago y manda la…, render_order_state(), update_order(), invalidate(), test_a_free_zone_confirms_with_zero_fee_and_says_gratis() (+24 more)

### Community 27 - "Proveedor Kapso"
Cohesion: 0.12
Nodes (32): provider_for(), Proveedor de envío del negocio: su `whatsapp_provider` o, si no lo tiene, el…, _business(), _hex(), kapso_settings(), fixture, Conexión por Kapso: firma de sus webhooks y envío por su proxy de Graph API. Lo…, _response() (+24 more)

### Community 28 - "Ruteo de impresora (tests)"
Cohesion: 0.15
Nodes (31): Impresora habilitada para el negocio, con su token en la caché de secretos…, seed_printer(), parametrize, Versión y actualización de la Raspberry desde el panel: van a la URL de SU…, _respond(), test_invalid_wifi_never_echoes_the_password(), test_raspberry_401_becomes_502_with_its_detail(), test_raspberry_down_or_slow() (+23 more)

### Community 29 - "Menú de hoy del admin"
Cohesion: 0.12
Nodes (28): _admin_phone(), get_today_menu(), mark_closed_today(), Guarda que el negocio NO abre hoy, para que la IA de clientes le avise a quien…, Deja como menú de HOY el último menú que el admin confirmó, sin mandar foto.…, Devuelve el menú del día vigente HOY, ya formateado en texto listo para…, reuse_last_menu(), datetime (+20 more)

### Community 30 - "Costo de domicilio"
Cohesion: 0.16
Nodes (30): add_delivery_zone(), Registra el valor del domicilio que cotizó el admin para un cliente cuya…, Agrega una zona de domicilio con tarifa fija, o actualiza la tarifa y los alias…, set_delivery_quote(), _conversation(), _delivery_fee(), Tarifa ya resuelta por el código (zona o cotización), o None., Determina la zona y el valor del domicilio de una dirección. Llámala cada vez… (+22 more)

### Community 31 - "Tools de pedido"
Cohesion: 0.11
Nodes (28): _add(), _apply(), _cart_item(), _find(), full_address(), _line(), load_cart(), _new_order() (+20 more)

### Community 32 - "Webhook entrante y firma"
Cohesion: 0.12
Nodes (29): content_event_id(), event_field(), event_timestamp(), phone_number_ids(), Utilidades PURAS del payload del webhook de WhatsApp: separar una entrega en…, `changes[0].field` de un payload (ej. "messages", "history")., Id determinista para eventos sin wamid: `field:` + hash del `value`. Sirve como…, Separa un webhook `history` (YA separado por split_events) en un payload por… (+21 more)

### Community 33 - "Domiciliarios"
Cohesion: 0.11
Nodes (26): Agente conversacional para los domiciliarios del restaurante (Google ADK). El…, _courier_context(), liquidar_comandas(), mi_resumen_de_hoy(), Tools del agente de domiciliarios. Quien escribe ya fue verificado como…, Reporta un problema con una entrega (no abren, la portería no responde, la…, Liquida las comandas que el domiciliario entregó hoy, a su nombre. Úsala cuando…, Lo que el domiciliario lleva liquidado hoy: comandas, efectivo y transferencia.… (+18 more)

### Community 34 - "Privacidad y Kapso"
Cohesion: 0.11
Nodes (20): Guarda el valor cotizado y se lo comunica al cliente. Devuelve True si el…, set_quote(), ask(), _business_name(), forget_customer(), gate(), has_consent(), policy_url() (+12 more)

### Community 35 - "Esquemas de configuración"
Cohesion: 0.12
Nodes (20): AdminPhoneNumbersUpdateRequest, check_unique_zones(), ConfigurationUpdateRequest, CourierEntry, CouriersUpdateRequest, CuentaTransferencia, DeliveryZone, FaqEntry (+12 more)

### Community 36 - "Contrato de impresión Raspberry"
Cohesion: 0.11
Nodes (26): `payment_method` viaja como campo aparte, sin tocar `text`: el servidor de la…, current_version(), get_health(), get_version(), post_print(), post_update(), post_wifi(), PrintJob (+18 more)

### Community 37 - "Secret Manager"
Cohesion: 0.12
Nodes (26): _cached_versions(), _get_client(), get_secret(), get_secret_or_none(), get_secret_versions(), invalidate(), _project(), put_secret() (+18 more)

### Community 38 - "Runbook y migración Edy Gourmet"
Cohesion: 0.13
Nodes (27): Comandas destination (printer or admin chat, orders_to_admin_chat), Comanda fallback to admin chat, admin_templates (UTILITY templates outside 24h window), Cloud Scheduler jobs (/admin/jobs/*), Cloudflare Access (panel.3b-technology.com login JWT), Deploy (tagged no-traffic revisions) and rollback, Device status (operativo / degradado / desconectado, 210s), devices collection (+19 more)

### Community 39 - "Sincronización de catálogo"
Cohesion: 0.16
Nodes (25): Actualiza el menú con el catálogo de WhatsApp Business del negocio (productos,…, sync_catalog(), FixedMenuItems, Productos que se suman TODOS los días al menú del día sin tener que aparecer en…, apply_catalog(), _changes(), fetch_catalog_products(), parse_price() (+17 more)

### Community 40 - "Jobs de reactivación"
Cohesion: 0.13
Nodes (20): Vuelve a mandar al cliente la foto del menú de hoy (su carta o el menú del día)…, send_menu_image(), Desde cuándo cuenta la inactividad de una conversación pausada: el último…, datetime, Barrido de reactivación automática del bot. Recorre conversaciones pausadas…, run_reactivation_sweep(), seed_conversation(), _paused_conversation() (+12 more)

### Community 41 - "Telemetría Raspberry"
Cohesion: 0.13
Nodes (24): lifespan(), _cmd(), current_commit(), device_id(), heartbeat_url(), _now(), Estado de esta Raspberry y heartbeat hacia el backend principal. El backend no…, DEVICE_ID del env, o uno estable derivado de /etc/machine-id (sin exponerlo). (+16 more)

### Community 42 - "Panel: configuración"
Cohesion: 0.09
Nodes (26): actions, BUSINESS_ID, commitAlias(), confirmDialog, cuentaDialog, cuentaForm, DEVICE_STATUS, EVENT_LABEL (+18 more)

### Community 43 - "Stock del menú"
Cohesion: 0.10
Nodes (25): active_menu(), _fixed_price(), Lo mismo que get_active_menu, para usarlo fuera de un turno del cliente (ej. el…, El precio solo se expone si está confirmado; si no, None -- el modelo nunca…, DailyStock, Un documento por negocio y día (id "<business_id>_<YYYY-MM-DD>"): lo que el…, apply_button(), confirmation() (+17 more)

### Community 44 - "Extractores visión y corrección"
Cohesion: 0.12
Nodes (21): build_genai_client(), apply_correction(), MenuCorrectionError, Exception, Aplica una corrección en texto libre a un menú ya extraído (borrador…, La corrección no se pudo aplicar (el modelo no devolvió JSON válido)., _maybe_add_date_mismatch_warning(), datetime (+13 more)

### Community 45 - "Acuerdos de pago y diff de menú"
Cohesion: 0.13
Nodes (20): Orders, format_cop(), Pesos con punto de miles ($18.500), o `none_text` si no hay precio., find_order_awaiting_authorization(), Acuerdos especiales de pago: tiquetera, pago mensual, "eso ya lo pagué", "la…, Deja el pedido esperando la decisión y le pregunta a la administradora.…, El pedido más reciente del cliente que espera la decisión, o None., Aplica la decisión de la administradora y se la comunica al cliente. Devuelve… (+12 more)

### Community 46 - "API Raspberry y túnel (tests)"
Cohesion: 0.12
Nodes (18): FakeQueue, Cola en memoria con la misma forma que cloud_tasks_client. `enqueue_event`…, main(), client(), _fake_git(), fixture, parametrize, Simula git: rev-parse devuelve heads en orden. (+10 more)

### Community 47 - "Worker y logging"
Cohesion: 0.18
Nodes (21): bind(), configure_logging(), _JsonFormatter, Logging del proceso: una línea JSON por registro en Cloud Run, texto en local.…, Agrega campos de correlación a los logs de la request actual., Idempotente. JSON si corre en Cloud Run (K_SERVICE), texto si no., _TextFormatter, device_heartbeat_task() (+13 more)

### Community 48 - "Formateo de menú"
Cohesion: 0.17
Nodes (23): MenuChange, _chunk_text(), format_carta_text(), format_confirmation_message(), _format_money(), _incluye_text(), date, JSON extraído (esquema menu_del_dia/otras_opciones) -> texto legible para… (+15 more)

### Community 49 - "Memoria de conversación (tests)"
Cohesion: 0.17
Nodes (18): _answer(), _call(), _CompactableService, _Doc, F2: la sesión ADK no crece sin límite. Una escritura por turno, sin los bytes…, Si el turno muere después de save_order, la historia debe mostrar que la tool…, _result(), _service() (+10 more)

### Community 50 - "Panel: app y UI"
Cohesion: 0.11
Nodes (23): allClients, applyFilter(), cargarNegocios(), countBadge, form, formMessage, handleSubmit(), renderClients() (+15 more)

### Community 51 - "Sesiones ADK en Firestore"
Cohesion: 0.16
Nodes (11): FirestoreSessionService, Any, Event, Session, Escribe lo que quedó pendiente del turno (ej. tools ya ejecutadas de un turno…, Deja solo `events` en la sesión y aplica `state_updates` (ej. el resumen de lo…, Session.model_dump(mode="json") sin los bytes de media de los eventos., _strip_media() (+3 more)

### Community 52 - "Reporte Excel dashboard"
Cohesion: 0.14
Nodes (20): Constantes y tipos compartidos por todo el paquete excel_report_service., build_dashboard_dataframe(), compute_dashboard_data(), fill_dashboard_template(), generate_dashboard_report(), _local_naive(), Any, Path (+12 more)

### Community 53 - "Plan de agentes IA"
Cohesion: 0.13
Nodes (16): SessionService de ADK respaldado en Firestore, para que el historial de una…, Red de seguridad sobre el texto final que un agente ADK va a mandarle a un…, Refactor de agentes IA (F0-F7), Transcripcion de audio, Rafagas y concurrencia, Reglas de negocio en Firestore (F6), Tools del cliente de 14 a 5, Regla determinista de imagen del menu (+8 more)

### Community 54 - "Agent runner"
Cohesion: 0.14
Nodes (12): Agent, AgentRunnerService, _app_name(), datetime, Session, Ejecución de agentes ADK: convierte un Agent ya construido + un mensaje de…, La sesión ADK de HOY para la conversación (ver _daily_session_id), creándola la…, Si la sesión pasó de conversation_memory.MAX_EVENTS, resume lo viejo y deja… (+4 more)

### Community 55 - "Almacenamiento de comprobantes"
Cohesion: 0.13
Nodes (19): _client(), datetime, Guarda en Cloud Storage la imagen de cada comprobante de transferencia, para…, URL del comprobante guardado, o None si no se pudo guardar (ya logueado). Nunca…, receipt_path(), store_receipt(), _normalize_reference(), BaseModel (+11 more)

### Community 56 - "Webhooks de coexistencia"
Cohesion: 0.18
Nodes (21): echo_payload(), _echo_at(), _envelope(), _history_payload(), _metadata(), datetime, Webhooks de coexistencia: history, smb_app_state_sync y el ORDEN de los echoes…, Prueba real 2026-10-19: desde el número del negocio se le mandó el link del… (+13 more)

### Community 57 - "Pedidos catálogo y agente"
Cohesion: 0.15
Nodes (19): La instancia del proceso. Los locks por conversación solo protegen si TODOS…, shared_agent_runner(), phone_for_humans(), El número del cliente para un aviso al equipo, o una nota si escribe con nombre…, _ask_admin_text(), _candidates(), confirm_code(), find_product() (+11 more)

### Community 58 - "Normalización de teléfonos"
Cohesion: 0.15
Nodes (18): is_own_order(), True si el pedido lo hizo el propio domiciliario como cliente., contact_phone(), is_username_id(), normalize_phone(), Teléfonos en un solo formato: solo dígitos, con indicativo de país, que es como…, BSUID de WhatsApp (ej. "CO.1780159176423859"): el cliente escribe con nombre de…, El número para que una persona (domiciliario, admin, comanda) llame o escriba… (+10 more)

### Community 59 - "Model pool"
Cohesion: 0.14
Nodes (14): acall(), call(), _error_code(), _Plan, pool(), Any, BaseException, Fallback entre modelos Gemini (MODEL_POOL) ante 429, 5xx o timeout. Se engancha… (+6 more)

### Community 60 - "Flujo E2E (tests)"
Cohesion: 0.20
Nodes (18): customer_message_payload(), fake_store(), fixture, Como en docker-compose: con el emulador, el worker no exige el token OIDC de…, sign(), _tasks_as_in_local_emulator(), _customers_already_accepted(), _messages() (+10 more)

### Community 61 - "Panel: configService (API)"
Cohesion: 0.21
Nodes (19): createBusiness(), createFixedItem(), deleteFixedItem(), errorMessage(), fromProducto(), getConfiguration(), getPrinterDevice(), listBusinesses() (+11 more)

### Community 62 - "Payload del webhook"
Cohesion: 0.26
Nodes (18): event_id(), Descompone una entrega en payloads de UN solo evento. Cada payload conserva la…, `wamid` de un payload YA separado (mensaje de cliente o echo). Es el…, split_events(), _envelope(), split_events debe separar TODO lo que Meta puede agrupar sin hacer desaparecer…, _handle_status_update ya recorre todos los statuses: separarlos duplicaría el…, test_batched_messages_are_split_one_per_payload() (+10 more)

### Community 63 - "Seguridad domicilios y Kapso"
Cohesion: 0.15
Nodes (15): Cliente genérico para la API de WhatsApp Cloud (Graph API de Meta). No es solo…, _warn_env_fallback(), Anti-replay (limitacion), Domicilios e incidencias (maquina de estados), Numeros en E.164, Webhook multi-tenant /webhook/kapso/{tenantKey}, Secret Manager por negocio, Nombres kapso-api-key--{businessId} (tipo primero) (+7 more)

### Community 64 - "Hardware Raspberry"
Cohesion: 0.17
Nodes (17): check_network(), check_printer(), get_printer(), print_text(), Acceso a la impresora térmica USB (ESC/POS) y chequeo de red., True si la impresora está conectada por USB., True si se puede abrir una conexión TCP (por defecto, al SSH de GitLab)., _usb_ids() (+9 more)

### Community 65 - "Raspberry README y panel"
Cohesion: 0.20
Nodes (18): Raspberry heartbeat POST /devices/heartbeat (every 60s), Printer device proxy (GET .../printer/device -> /version, POST .../printer/update -> /update), Webhook service j2ys-lt-plain (public), impresora-edygourmet.3b-technology.com, Cloudflare tunnel (update_tunnel.py adds ingress rule), backend_raspberry README, GitLab self-update via SSH deploy key, GET /health (+10 more)

### Community 66 - "Panel: validadores de config"
Cohesion: 0.20
Nodes (18): addAdmin(), anyDirty(), applyPlan(), clone(), discardSection(), getPath(), isDirty(), load() (+10 more)

### Community 67 - "Notas de voz (tests)"
Cohesion: 0.21
Nodes (12): AgentReply, DummyHandlers, Reemplaza get_vertical_handlers: rol fijo y un agente que nunca se ejecuta…, test_a_flush_waiting_in_the_queue_does_not_answer_if_the_owner_replied(), _build_context(), asyncio, test_authorized_admin_image_is_intercepted_before_the_agent(), test_unauthorized_number_image_never_reaches_menu_orchestrator() (+4 more)

### Community 68 - "Procesador de audio"
Cohesion: 0.17
Nodes (12): AudioProcessingError, AudioProcessor, ProcessedAudio, Exception, Descarga, valida y transcribe las notas de voz entrantes de WhatsApp. La…, El audio no se pudo descargar, leer, o excede la duración máxima., Descarga un audio de WhatsApp Cloud API por su media_id y valida que dure como…, Texto de la nota de voz (una llamada corta al modelo, solo en turnos con… (+4 more)

### Community 69 - "Configuración por cliente y verticales"
Cohesion: 0.15
Nodes (15): is_registered(), Registro de verticales de negocio. Cada vertical (restaurante, odontología,…, Si la vertical tiene agente: una sin registrar no contesta nunca., VerticalHandlers, Couriers (PUT .../couriers), delivery_zones, Fixed menu items (/admin/restaurant/fixed-menu-items), Menu reminder (recordatorio_menu_hora) (+7 more)

### Community 70 - "Servicio de reporte Excel"
Cohesion: 0.19
Nodes (12): compute_dashboard_summary(), ExcelReportService, Any, Path, Genera el Excel del negocio a partir de los pedidos de Firestore. Hay un solo…, Todo Excel sale de la plantilla: los pedidos (Orders) del rango se pegan en su…, ExcelReportSpec, Contrato que usará la IA cuando sepa qué pidió el cliente. (+4 more)

### Community 71 - "Repositorio Firestore de reportes"
Cohesion: 0.26
Nodes (6): FirestoreReportRepository, Any, Para personas (admin, Excel): `phone_number` es su celular real (el que dio, si…, Consulta Firestore y normaliza los modelos a `dict`s planos., Por su número de WhatsApp, su código de usuario o, si escribe con usuario, por…, El cliente al que se refiere el admin: por teléfono, código de usuario o…

### Community 72 - "Webhook multi-tenant"
Cohesion: 0.27
Nodes (16): clear_cache(), _body(), env(), _post(), fixture, POST /webhook/kapso/{tenantKey}: cada negocio con su URL y su secreto. Lo que…, test_bad_signature_is_401_and_enqueues_nothing(), test_event_from_another_tenants_number_is_403() (+8 more)

### Community 73 - "Imagen de menú (tests)"
Cohesion: 0.28
Nodes (16): _context(), _menu(), Regla de la imagen del menú: sale sola la primera vez del día (o si el admin…, Caso real: "quiero ordenar" con menú publicado respondía el menú en texto.…, Hoy" en la zona del negocio, calculado en cada test: una constante del módulo…, Antes se guardaba la foto del menú del día también en carta_media_id: al día…, test_auto_sends_once_per_version(), test_catalog_link_is_sent_as_text_instead_of_images() (+8 more)

### Community 74 - "Cliente WhatsApp (tests)"
Cohesion: 0.18
Nodes (14): client(), _no_retry_wait(), fixture, Lo que se protege es cómo se interpreta la respuesta de la Graph API: el wamid…, Regresión: el log solo traía el cuerpo crudo, sin código HTTP ni a qué número;…, _response(), test_a_rejected_send_is_not_retried_and_logs_metas_code(), test_a_transient_meta_error_is_retried() (+6 more)

### Community 75 - "Dos tenants (tests)"
Cohesion: 0.22
Nodes (14): Lo que el agente de clientes ve en CADA llamada al modelo: las reglas del…, render_state(), _ctx(), Reglas del negocio en Firestore: lo que el admin configura llega a la…, test_editing_the_configuration_refreshes_the_cache(), test_payment_methods_and_minimum_come_from_the_configuration(), test_the_profile_reaches_the_instruction(), Dos negocios con configuraciones distintas no se mezclan: cada uno ve su tono,… (+6 more)

### Community 76 - "API de ítems fijos del menú"
Cohesion: 0.22
Nodes (13): create_fixed_menu_item(), delete_fixed_menu_item(), _get_fixed_item_or_404(), list_fixed_menu_items(), get, post, update_fixed_menu_item(), FixedMenuItemCreateRequest (+5 more)

### Community 77 - "Diff de menú"
Cohesion: 0.19
Nodes (12): _acompanamientos_key(), diff_menus(), _format_money(), Compara el menú recién extraído contra el último menú ACTIVO del mismo negocio…, Sin menú anterior (primera carga del negocio), no hay nada que comparar --…, El último menú confirmado del negocio (referencia de formato y base del diff),…, test_envios_and_telefono_changes_are_flagged(), test_identical_menu_has_no_changes() (+4 more)

### Community 78 - "Recordatorios de menú"
Cohesion: 0.26
Nodes (15): datetime, True si `now_local` cae en [hora, hora + REMINDER_WINDOW_MINUTES)., remind_missing_daily_menus(), reminder_due(), reminder_text(), asyncio, parametrize, test_each_business_is_reminded_once_at_its_own_hour() (+7 more)

### Community 79 - "Zonas y cotizaciones"
Cohesion: 0.18
Nodes (15): parse_quote_command(), domicilio $4.000 para 3001234567" / "4k Camilo" / "4000" -> (valor, a quién):…, find_zone(), matching_zones(), _mentions(), normalize(), Dirección escrita por el cliente -> zona de domicilio del catálogo. Los…, Todas las zonas cuyo nombre o algún alias aparece en la dirección. (+7 more)

### Community 80 - "Generación de Excel (tests)"
Cohesion: 0.21
Nodes (13): dashboard_template(), generate(), make_dashboard_template(), fixture, Genera de verdad el Excel (sin mockear generate_report) y revisa el archivo…, Datos que ejercitan todas las ramas: dos productos, un cancelado, días de…, Sea cual sea el report_type pedido, el Excel es la plantilla con los pedidos…, Plantilla mínima con lo que trae la real: tabla `datos` con una fila de ejemplo… (+5 more)

### Community 81 - "Doc de handoff"
Cohesion: 0.19
Nodes (11): datetime, `at`: momento real del mensaje que reactiva (o ahora, si es una acción manual).…, True si ya pasaron HANDOFF_QUICK_REACTIVATION_SECONDS sin un mensaje humano…, `at`: momento real del evento que pausa (ej. el timestamp del echo según…, Reactivacion automatica por inactividad (30s), Handoff bot <-> humano (Coexistencia), Campos Firestore de handoff (bot_activo, pausado_desde...), Barrido reactivate-bots (Cloud Scheduler) (+3 more)

### Community 82 - "Elección de zona"
Cohesion: 0.21
Nodes (14): candidate_zones(), Zonas que pueden ser el barrio del cliente cuando find_zone no dio una sola: su…, Barrio corto o parecido a varias zonas ("Belén"): el cliente elige en una lista…, Sesión con el pedido esperando que el cliente elija en la lista., _reply(), _state(), test_a_barrio_like_no_zone_still_goes_to_the_admin(), test_an_ambiguous_barrio_sends_the_list_instead_of_asking_the_admin() (+6 more)

### Community 83 - "Tenants y página de privacidad"
Cohesion: 0.20
Nodes (13): _load(), privacy_page(), get, HTMLResponse, Página pública de la política de datos de cada negocio: GET…, render_policy(), _firestore(), _lookup() (+5 more)

### Community 84 - "Firma de webhook"
Cohesion: 0.24
Nodes (13): _hmac_hex(), Validación de la firma de los webhooks entrantes (Meta directo o Kapso). Meta…, True si `signature_header` (valor del header X-Hub-Signature-256) coincide con…, Igual que verify_meta_signature pero con el formato de Kapso: hex sin prefijo.…, verify_kapso_signature(), verify_meta_signature(), test_empty_secrets_reject_even_matching_empty_key_hmacs(), _sign() (+5 more)

### Community 85 - "README y despliegue del panel"
Cohesion: 0.20
Nodes (15): WhatsApp channel settings (number, provider, Kapso credentials, webhook URL, bot status), Kill switch bot-status (businesses.status), opening_hours, AgentRunnerService (ADK Runner, in-memory sessions), businesses collection (tenant), configurations collection (per-business config), backend_principal README, Firestore data model (businesses, configurations, customers, conversations, messages, menu_items, orders, webhook_events, print_jobs) (+7 more)

### Community 86 - "Evals de conversación"
Cohesion: 0.25
Nodes (13): Chat, _products(), Casos reales de la sección 2.12 de PLAN_AGENTES_IA.md, contra el modelo real.…, test_a_burst_gets_one_reply_that_covers_everything(), test_a_fact_said_early_is_kept_until_the_end(), test_a_preparation_note_reaches_the_ticket(), test_a_voice_correction_changes_the_order(), test_changing_their_mind_three_times_keeps_only_the_last() (+5 more)

### Community 87 - "Webhook entry (tests)"
Cohesion: 0.23
Nodes (11): _message_body(), El ingress solo verifica firma, separa y encola. Lo importante acá es lo que NO…, Regresión: los statuses se descartaban sin mirar, y "failed" es la única forma…, _signed(), test_a_failed_delivery_status_is_logged_as_an_error(), test_each_message_is_enqueued_once_with_its_wamid(), test_enqueue_failure_answers_502_so_meta_retries(), test_invalid_signature_is_rejected_and_nothing_is_enqueued() (+3 more)

### Community 88 - "Worker entry (tests)"
Cohesion: 0.13
Nodes (9): parametrize, El worker tiene dos rutas y la diferencia entre ellas es el diseño:…, Regresión: si Cloud Tasks fallaba al agendar el flush, el mensaje quedaba…, El control de acceso real es IAM/OIDC en Cloud Run, no esta cabecera: el…, Un error fuera de la ingesta puede ocurrir después de haberle escrito al…, test_a_retryable_failure_asks_cloud_tasks_to_retry_up_to_a_cap(), test_an_unexpected_ingest_error_answers_200(), test_if_the_flush_cannot_be_scheduled_the_task_answers_itself() (+1 more)

### Community 89 - "Panel: validadores"
Cohesion: 0.19
Nodes (13): FEATURE_LABEL, isNonNegativeInt(), normalize(), PHONE_RE, validateAccesos(), validateAlias(), validateComandas(), validateCuenta() (+5 more)

### Community 90 - "API de conversaciones"
Cohesion: 0.29
Nodes (12): close_conversation(), get_conversation(), _get_conversation_or_404(), pause_bot(), get, post, Pausa manual: útil para que un agente humano tome la conversación…, Cierra la conversación Y reactiva el bot: la próxima vez que el cliente… (+4 more)

### Community 91 - "API de jobs"
Cohesion: 0.22
Nodes (12): check_unanswered_job(), delivery_sweep_job(), expire_pending_daily_menus_job(), post, Endpoints pensados para dispararse desde un scheduler externo (Cloud Scheduler…, Cada 5 min desde Cloud Scheduler: loguea ERROR por cada conversación de un…, Cada 10 min desde Cloud Scheduler: presume entregados los pedidos que pasaron…, reactivate_bots() (+4 more)

### Community 92 - "Flujos del router (tests)"
Cohesion: 0.29
Nodes (13): ingest_and_reply(), Lo que hace el worker con sus dos tareas, sin la cola en medio:…, _build_context(), asyncio, datetime, parametrize, Regresión (prueba real 2026-10-05): la IA pedía un humano por "baja confianza"…, Prueba real 2026-10-16: una respuesta de la encargada pausó el chat del… (+5 more)

### Community 93 - "Agent runner (tests)"
Cohesion: 0.32
Nodes (12): _daily_session_id(), Una sesión ADK por conversación y por día del negocio. Antes la sesión era la…, La sesión ADK guarda business_id/customer_id/conversation_id en su state (de…, Regresión: sin tope, un Vertex colgado retenía el lock de la conversación hasta…, _runner(), _session(), test_a_hung_turn_ends_with_the_fallback_reply(), test_a_new_day_starts_a_new_session_without_the_old_history() (+4 more)

### Community 94 - "Memoria de conversación"
Cohesion: 0.19
Nodes (12): append_current_state(), Event, Memoria de la conversación: la sesión ADK guarda solo los últimos eventos y un…, Resumen nuevo (anterior + eventos viejos). Lanza si el modelo falla: quien…, Instrucción del agente = prompt fijo + resumen de lo anterior, si hay. El…, before_model_callback: el estado actual (ej. el pedido en curso) va AL FINAL de…, Índice desde el que se conservan los eventos, o None si no hace falta…, _render() (+4 more)

### Community 95 - "Onboarding coexistencia y despliegue"
Cohesion: 0.19
Nodes (10): Pipeline Cloud Build: test -> build -> deploy sin trafico, Worker --max-instances=1, Emulador de Cloud Tasks (aertje), META_ES_COEXISTENCE_FEATURE_TYPE, WhatsApp Embedded Signup v4, Cola de historial lenta (maxScale=1), App de Meta restaurante-edy-automatizacion, Servicio webhook j2ys-lt-plain (Cloud Run) (+2 more)

### Community 96 - "Planes comerciales"
Cohesion: 0.27
Nodes (11): Registra si llegó o no la transferencia de un cliente, y se lo comunica al…, validar_transferencia(), find_order_awaiting_validation(), El pedido más reciente del cliente con la transferencia sin validar: con el…, _admin_decision_mocks(), Planes comerciales: cada función bloqueada se corta en el backend (tools,…, _set_plan(), test_admin_approves_and_without_printer_in_the_plan_the_ticket_goes_to_the_admin_chat() (+3 more)

### Community 97 - "Privacidad (tests)"
Cohesion: 0.40
Nodes (9): _click(), _ingest(), _new_customer(), Autorización de datos (Ley 1581): se pide antes de atender; sin "Acepto" no…, test_accepting_answers_what_they_had_written(), test_customers_who_accepted_and_the_team_are_not_asked(), test_declining_deletes_what_was_stored(), test_declining_keeps_the_customer_if_they_have_orders() (+1 more)

### Community 98 - "Descarga de medios"
Cohesion: 0.24
Nodes (6): MenuImageDownloader, MenuImageDownloadError, Exception, Path, Descarga y valida la imagen del menú del día enviada por un admin. Mismo patrón…, La imagen no se pudo descargar, verificar, o excede el tamaño máximo.

### Community 99 - "Credenciales Kapso (tests)"
Cohesion: 0.42
Nodes (8): _fake_put(), Alta de las credenciales de Kapso de un negocio: los valores van a Secret…, test_requires_the_admin_api_key(), test_rotating_one_value_keeps_the_other(), test_short_or_empty_values_are_rejected_without_echoing_them(), test_stores_only_references_and_never_returns_the_values(), test_unknown_business_is_404(), _url()

### Community 100 - "Kill switch"
Cohesion: 0.36
Nodes (8): _context(), _ingest(), Kill switch por negocio (Businesses.status): apagado, los mensajes se guardan…, test_a_paused_business_stores_the_message_without_ai_and_others_keep_working(), test_after_reactivating_only_new_messages_are_answered(), test_bot_status_endpoint_records_who_and_when(), test_bot_status_requires_actor_and_a_known_status(), test_flush_of_a_paused_business_never_calls_the_agent()

### Community 101 - "Handoff humano tool"
Cohesion: 0.36
Nodes (7): Pasa la conversación a una persona del equipo. Es el ÚLTIMO recurso: el cliente…, request_human_handoff(), parametrize, request_human_handoff es el último recurso: solo pasa a una persona si el…, Regresión (prueba real 2026-10-05): con "baja_confianza" la IA pasaba a un…, test_doubt_or_any_other_reason_keeps_the_ai_attending(), test_real_reasons_register_the_handoff()

### Community 102 - "Reply guard"
Cohesion: 0.36
Nodes (7): guard_reply(), _looks_leaked(), Si `text` parece razonamiento filtrado o es demasiado largo, pide un resumen…, parametrize, Fugas de configuración: el caso real terminó con el bot confirmando que existe…, test_normal_replies_pass_untouched(), test_talking_about_its_own_setup_is_replaced_by_the_redirect()

### Community 103 - "Secretos (tests)"
Cohesion: 0.25
Nodes (4): clear_cache(), FakeSecretManager, manager(), fixture

### Community 105 - "Panel: helpers UI"
Cohesion: 0.29
Nodes (8): renderCuentas(), renderProductos(), renderZonas(), showBusinessPicker(), showState(), formatCOP(), maskNumber(), escapeHtml()

### Community 106 - "Módulo test_admin_reactivate_customer_chat"
Cohesion: 0.52
Nodes (6): Reactiva el bot para un cliente puntual después de que la encargada terminó de…, reactivate_customer_chat(), _seed_customer_and_conversation(), test_reactivates_a_paused_conversation(), test_reports_customer_not_found(), test_reports_not_paused_when_already_active()

### Community 107 - "Logging setup"
Cohesion: 0.33
Nodes (3): En Cloud Run cada log debe ser UNA línea JSON con severidad y con el contexto…, _record(), test_metric_fields_go_into_the_json_line()

### Community 108 - "Límites de servicios"
Cohesion: 0.29
Nodes (3): Qué expone cada servicio y qué carga. Son dos servicios con su propio…, El valor de separar el webhook es que arranque sin el peso del worker, y su…, test_ingress_does_not_load_the_heavy_stack()

### Community 109 - "Túnel Raspberry"
Cohesion: 0.48
Nodes (5): Agrega o actualiza la ruta del backend en el config de cloudflared. No borra el…, upsert_ingress(), test_actualiza_si_ya_existe(), test_agrega_antes_del_catch_all_sin_tocar_lo_demas(), test_sin_ingress_crea_catch_all()

### Community 110 - "Panel: config extra"
Cohesion: 0.38
Nodes (7): ago(), loadPrinterDevices(), loadPrinterEvents(), loadPrinterVersion(), printerSaved(), testPrint(), when()

### Community 111 - "Panel: páginas HTML"
Cohesion: 0.33
Nodes (7): Plan section (plan table), Backend connection status pill, Business list table (WhatsApp, plan, bot status), index.html (panel), Plan selector (basico/avanzado/premium), Register business form (name, vertical, plan, WhatsApp number/id, Kapso credentials), panel_central frontend README (empty)

### Community 112 - "Módulo __init__"
Cohesion: 0.33
Nodes (4): Punto de entrada de la vertical de restaurante. El backend resuelve el rol del…, Agente conversacional para clientes del restaurante, construido con Google ADK.…, Registra lo que dijo el cliente sobre un pedido que ya confirmó hoy. Args:…, report_delivery_update()

### Community 113 - "Módulo test_whatsapp_format"
Cohesion: 0.47
Nodes (5): **negrita** de markdown -> *negrita* de WhatsApp. El modelo escribe markdown…, to_whatsapp_format(), WhatsApp usa *un asterisco* para negrita; el **doble** de markdown se le…, test_markdown_bold_becomes_whatsapp_bold(), test_text_without_markdown_is_unchanged()

### Community 114 - "Módulo firestore_repository"
Cohesion: 0.47
Nodes (5): as_datetime(), datetime, ZoneInfo, Acceso a Firestore para el servicio de reportes Excel. Aísla toda la conexión y…, Fecha en `tz`; una fecha sin zona se interpreta directamente en `tz`. None si…

### Community 115 - "Módulo set_reminder_hour"
Cohesion: 0.40
Nodes (5): main(), planned_changes(), Pone recordatorio_menu_hora = "09:30" a las Configurations que hoy reciben el…, [(doc_id, {"recordatorio_menu_hora": "09:30"})] de las que cambian., test_the_migration_keeps_today_s_reminder_only_where_it_was_sent()

### Community 116 - "Módulo test_payment_flow"
Cohesion: 0.33
Nodes (3): _FakeGenaiClient, _FakeModels, Como google.genai.Client: al liberarse cierra su conexión, y un request con la…

### Community 117 - "Módulo landing"
Cohesion: 0.40
Nodes (4): landing(), get, HTMLResponse, Landing pública mínima ("próximamente"), montada en el servicio de webhook…

### Community 118 - "Cliente de impresora"
Cohesion: 0.40
Nodes (3): PrinterClient, Cliente para mandar texto a la Raspberry / impresora de UN negocio., test_printer_payload_carries_the_payment_method_next_to_the_text()

### Community 119 - "IAM ops"
Cohesion: 0.60
Nodes (3): project_role(), sa(), iam.sh script

### Community 120 - "Secretos a refs"
Cohesion: 0.90
Nodes (4): env_value(), run(), secrets_to_refs.sh script, to_secret()

### Community 122 - "Alertas ops"
Cohesion: 0.83
Nodes (3): channel_for(), policy(), alerts.sh script

### Community 123 - "Módulo ?"
Cohesion: 0.50
Nodes (4): test_each_turn_logs_its_metrics(), fixture, runner(), InMemorySessionService

### Community 124 - "Módulo configuracion"
Cohesion: 0.50
Nodes (4): confirmar(), confirmPlanChange(), updateRaspberry(), lostFeatures()

## Ambiguous Edges - Review These
- `panel_central frontend README (empty)` → `index.html (panel)`  [AMBIGUOUS]
  panel_central/frontend/README.md · relation: conceptually_related_to

## Knowledge Gaps
- **45 isolated node(s):** `deploy.sh script`, `deploy_panel.sh script`, `firestore_backups.sh script`, `j2ys-chat-automatization`, `backend-rasperry` (+40 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 850 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **15 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `panel_central frontend README (empty)` and `index.html (panel)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `seed_business()` connect `Tests API de config del panel` to `Seguimiento de domicilios`, `Incidencias de entrega`, `Orquestador menú y handoff`, `Servicio menú del día`, `Pedidos por catálogo WhatsApp`, `Enums y pipeline entrante`, `Esquemas Firestore`, `Extracción de menú por imagen`, `Cloud Tasks y auth de tareas`, `Heartbeat de dispositivos`, `Tools del admin`, `Menú activo y stock (tests)`, `Flujo de pago del cliente`, `Handoff bot/humano`, `Horarios y perfil del negocio`, `Trabajos de impresión`, `Flush de conversación`, `Comprobantes de transferencia`, `Estado del pedido`, `Ruteo de impresora (tests)`, `Menú de hoy del admin`, `Costo de domicilio`, `Sincronización de catálogo`, `Jobs de reactivación`, `Stock del menú`, `Acuerdos de pago y diff de menú`, `Webhooks de coexistencia`, `Normalización de teléfonos`, `Flujo E2E (tests)`, `Notas de voz (tests)`, `Servicio de reporte Excel`, `Imagen de menú (tests)`, `Dos tenants (tests)`, `Recordatorios de menú`, `Generación de Excel (tests)`, `Elección de zona`, `Evals de conversación`, `Flujos del router (tests)`, `Agent runner (tests)`, `Planes comerciales`, `Privacidad (tests)`, `Credenciales Kapso (tests)`, `Kill switch`, `Módulo test_admin_reactivate_customer_chat`?**
  _High betweenness centrality (0.091) - this node is a cross-community bridge._
- **Why does `Businesses` connect `Orquestador menú y handoff` to `Seguimiento de domicilios`, `Incidencias de entrega`, `Servicio menú del día`, `Pedidos por catálogo WhatsApp`, `Enums y pipeline entrante`, `API admin de negocios`, `Esquemas Firestore`, `Extracción de menú por imagen`, `Tools del admin`, `Menú activo y stock (tests)`, `Router de eventos webhook`, `Tests API de config del panel`, `Flujo de pago del cliente`, `Handoff bot/humano`, `Horarios y perfil del negocio`, `Trabajos de impresión`, `Comprobantes de transferencia`, `Ruteo de impresora (tests)`, `Costo de domicilio`, `Domiciliarios`, `Privacidad y Kapso`, `Sincronización de catálogo`, `Jobs de reactivación`, `Extractores visión y corrección`, `Acuerdos de pago y diff de menú`, `Almacenamiento de comprobantes`, `Pedidos catálogo y agente`, `Flujo E2E (tests)`, `API de ítems fijos del menú`, `Recordatorios de menú`, `README y despliegue del panel`, `Planes comerciales`, `Credenciales Kapso (tests)`, `Kill switch`, `Módulo __init__`?**
  _High betweenness centrality (0.089) - this node is a cross-community bridge._
- **Why does `configuracion.html (panel)` connect `Configuración por cliente y verticales` to `Raspberry README y panel`, `Runbook y migración Edy Gourmet`, `Panel: configuración`, `Panel: páginas HTML`, `README y despliegue del panel`?**
  _High betweenness centrality (0.074) - this node is a cross-community bridge._
- **Are the 83 inferred relationships involving `Businesses` (e.g. with `avisar_tiempo_entrega()` and `request_stock_change()`) actually correct?**
  _`Businesses` has 83 INFERRED edges - model-reasoned connections that need verification._
- **Are the 79 inferred relationships involving `Orders` (e.g. with `avisar_tiempo_entrega()` and `print_order_ticket()`) actually correct?**
  _`Orders` has 79 INFERRED edges - model-reasoned connections that need verification._
- **Are the 60 inferred relationships involving `Customers` (e.g. with `_admin_phone()` and `avisar_tiempo_entrega()`) actually correct?**
  _`Customers` has 60 INFERRED edges - model-reasoned connections that need verification._