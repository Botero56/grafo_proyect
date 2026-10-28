"""
db.py

Modelos de Firestore (fireo) + inicialización, todo en un solo archivo.

Equivalente a create_db_tables() de SQLModel, pero adaptado a Firestore:
Firestore NO requiere crear colecciones de antemano, se crean solas al
hacer el primer .save(). init_db() solo conecta el cliente a fireo.
"""

import fireo
from google.cloud import firestore
from fireo.models import Model
from fireo.fields import TextField, NumberField, DateTime,BooleanField,MapField,IDField,ListField


# ----------------------------------------------------------------------
# MODELOS
# ----------------------------------------------------------------------

class Businesses(Model):

    bussiness_id = IDField(include_in_document=True)
    name = TextField()
    legal_name = TextField()
    bussiness_vertical =TextField()
    phone = TextField()
    whatsapp_phone_number = TextField()
    whatsapp_phone_number_id = TextField()

    # --- Embedded Signup / coexistencia (ver app/services/onboarding/) ---
    # Campos aditivos de cuando existía el onboarding propio (ya retirado:
    # los números se conectan por Kapso). Se conservan por los datos viejos.
    waba_id = TextField()
    whatsapp_onboarding_status = TextField()  # WhatsAppOnboardingStatus
    coexistence_enabled = BooleanField()
    whatsapp_connected_at = DateTime()
    # Por dónde se envía desde este número: "meta" (Graph API con nuestra
    # app) o "kapso" (número conectado por un setup link de Kapso). None =
    # WhatsAppsettings.WHATSAPP_PROVIDER. Permite migrar número por número.
    whatsapp_provider = TextField()

    # --- Kapso por tenant (ver app/services/secrets/ y SECURITY.md) ---
    # Solo REFERENCIAS a Secret Manager ("projects/<p>/secrets/<id>"), nunca
    # el valor. Las escribe PUT /admin/businesses/{id}/kapso-credentials, no
    # el PATCH de configuración. None = se usa la variable de entorno global
    # (migración).
    kapso_api_key_secret = TextField()
    kapso_webhook_secret_secret = TextField()
    # Clave de la URL del webhook por tenant (/webhook/kapso/{clave}). En
    # claro a propósito: el front muestra la URL; la barrera es la firma.
    webhook_tenant_key = TextField()
    webhook_tenant_key_rotated_at = DateTime()

    address = TextField()

    # BusinessStatus. Distinto de "active" = bot apagado (kill switch): los
    # mensajes se guardan pero no pasan por la IA. Lo cambia
    # PUT /admin/businesses/{id}/bot-status, que registra quién y cuándo.
    status = TextField()
    bot_status_changed_at = DateTime()
    bot_status_changed_by = TextField()
    bot_status_reason = TextField()

    # Plan comercial (app/services/plans.py): "basico", "avanzado" o
    # "premium". None = premium (negocios de antes de los planes). Lo cambia
    # PUT /admin/businesses/{id}/plan, que registra quién y cuándo.
    plan = TextField()
    plan_changed_at = DateTime()
    plan_changed_by = TextField()

    created_at = DateTime()
    updated_at = DateTime()

    class Meta:
        collection_name = "businesses"


class Customers(Model):
    customer_id = IDField(include_in_document=True)
    business_id = TextField()

    name = TextField()
    # Dirección de WhatsApp a la que se le responde: su número, o su BSUID
    # ("CO.178...") si escribe con nombre de usuario (ver phones.is_username_id).
    phone_number = TextField()
    whatsapp_user_id = TextField()
    # Número que dio un cliente que escribe con nombre de usuario (Meta no
    # lo comparte). Solo para que una persona lo contacte; las respuestas
    # siguen yendo a su BSUID. Ver phones.contact_phone.
    contact_phone = TextField()

    address = TextField()
    notes = TextField()

    status = TextField()

    created_at = DateTime()
    updated_at = DateTime()
    last_interaction_at = DateTime()

    # Domiciliario que eligió escribir como cliente: día de servicio
    # ('YYYY-MM-DD') en que vale ese modo (ver role_resolver). Se borra al
    # confirmar su pedido (couriers.end_customer_mode).
    courier_as_customer_date = TextField()

    # Autorización de datos (Ley 1581): cuándo tocó "Acepto" y qué versión
    # de la política aceptó. Ver app/services/privacy.py.
    data_consent_at = DateTime()
    data_consent_version = TextField()
    # Último envío de los botones (para no repetirlos en cada mensaje).
    data_consent_asked_at = DateTime()

    class Meta:
        collection_name = "customers"  # subcolección: usar parent=business.key

class Conversations(Model):
    conversation_id = IDField(include_in_document=True)
    business_id = TextField()
    customer_id = TextField()

    channel = TextField()
    status = TextField()

    started_at = DateTime()
    last_message_at = DateTime()
    closed_at = DateTime()

    last_message_id = TextField()
    active_order_id = TextField()

    # --- Handoff bot <-> humano (coexistencia) ---
    # Campos aditivos: los documentos existentes se leen con su default
    # (bot_activo=True, resto None), sin necesidad de migración -- mismo
    # patrón ya usado en este archivo (ver whatsapp_phone_number_id).
    bot_activo = BooleanField(default=True)
    # Uno de HandoffMotivo (app.schemas.enum_schemas), o None si el bot
    # está activo.
    motivo_handoff = TextField()
    agente_asignado = TextField()
    pausado_desde = DateTime()
    pausado_hasta = DateTime()
    # Último mensaje del CLIENTE (no cuenta ecos de humano ni respuestas de
    # la IA) -- es lo que abre/extiende la ventana de 24h de WhatsApp.
    ultimo_mensaje_cliente_at = DateTime()
    # Último mensaje enviado por un HUMANO desde la app (echo) -- usado por
    # el job de reactivación por inactividad, ver
    # app/services/conversation/reactivation_job.py.
    ultimo_mensaje_humano_at = DateTime()
    # Cuándo se reactivó el bot por última vez (momento del mensaje que lo
    # reactivó, o de la acción manual). Cloud Tasks no garantiza orden: un
    # echo ANTERIOR a esta marca que llegue tarde no debe volver a pausar.
    bot_reactivado_at = DateTime()
    # Marca de agua del agrupado por cola: `created_at` del último mensaje
    # del cliente que la IA YA contestó. Todo mensaje entrante posterior a
    # esta marca está pendiente de respuesta.
    #
    # Es lo que hace idempotente al flush diferido (ver
    # app/services/messaging/inbound_pipeline.py::flush_conversation): varias tareas de
    # flush para la misma conversación son normales -- cada mensaje agenda
    # la suya --, y la primera que corre se lleva todo lo pendiente y avanza
    # la marca; las demás no encuentran nada y no hacen nada. None (campo
    # ausente en documentos viejos) = nunca se respondió, se toma todo lo
    # que haya.
    ultimo_mensaje_respondido_at = DateTime()
    # "<fecha>:<versión>" de la última imagen del menú enviada al cliente
    # (ver DailyMenuService.current_menu_image). Distinta a la vigente = hay
    # que mandarla: primer turno del día o el admin publicó otra.
    menu_enviado = TextField()

    # --- Domicilio del pedido en curso ---
    # Lo resuelve el código (catálogo de zonas o cotización de un humano),
    # nunca la IA: update_order y save_order lo leen de acá. Ver
    # app/services/delivery/.
    delivery_address = TextField()
    delivery_zone = TextField()
    delivery_fee = NumberField()
    delivery_status = TextField()  # DeliveryStatus

    class Meta:
        collection_name = "conversations"  # subcolección: usar parent=business.key



class DailyMenus(Model):
    """Menú del día extraído de una imagen que manda un admin autorizado.

    Colección unificada: antes existían dos ("menu_items", un catálogo
    plano viejo sin relación con la imagen del admin, y "daily_menus", este
    modelo). Se unificaron en una sola colección Firestore ("menu_items")
    porque tener dos lugares distintos hablando de "el menú" generaba
    confusión real (2026-09-22) -- el catálogo plano viejo tenía datos de
    prueba sin conexión con lo que el admin sube por foto. Se migraron los
    documentos reales de "daily_menus" hacia "menu_items" y se borraron los
    del catálogo plano (ver backups/ en el repo local, no versionados). El
    nombre de la clase Python (`DailyMenus`) se mantiene tal cual -- sigue
    describiendo bien qué es cada documento -- solo cambió el nombre de la
    colección física en Firestore.

    Un documento = una foto de menú procesada. `fecha_vigencia` es un string
    'YYYY-MM-DD' en la zona horaria del negocio (DEFAULT_TZ, ver
    app.services.excel_report_service.constants) -- string y no DateTime
    para poder filtrar por igualdad exacta de día sin líos de rango horario.
    Ver app/services/daily_menu/ para el flujo completo.
    """

    daily_menu_id = IDField(include_in_document=True)
    business_id = TextField(required=True)
    admin_phone = TextField()

    # Versión del esquema de `extracted_menu`. Sube cuando cambia la forma
    # del JSON (ver app/services/daily_menu/vision_extractor.py -- versión
    # actual: 3, esquema menu_del_dia/acompanamientos/otras_opciones).
    schema_version = NumberField(required=True)

    fecha_vigencia = TextField(required=True)
    # Uno de DailyMenuStatus (app.schemas.enum_schemas): pendiente_confirmacion,
    # activo, reemplazado, descartado.
    status = TextField(required=True)

    # --- Origen ---
    source_media_id = TextField()
    # La misma foto subida a WhatsApp para reenviarla a los clientes: al
    # confirmar pasa a Configurations.carta_media_id.
    menu_media_id = TextField()
    source_image_path = TextField()
    source_sha256 = TextField()
    caption = TextField()

    # --- Extracción ---
    # ExtractedMenu.model_dump() -- ver el esquema exacto en
    # app/services/daily_menu/vision_extractor.py.
    extracted_menu = MapField()

    # Texto de correcciones aplicadas, en orden, para auditoría.
    correction_history = ListField(default=[])

    created_at = DateTime()
    confirmed_at = DateTime()
    discarded_at = DateTime()
    # Marca cuándo se mandó el único recordatorio de "sigues sin confirmar"
    # -- ver app/services/daily_menu/reminders.py. None = no se ha mandado.
    reminder_sent_at = DateTime()

    class Meta:
        collection_name = "menu_items"


class FixedMenuItems(Model):
    """Productos que se suman TODOS los días al menú del día sin tener que
    aparecer en la imagen (ej. bebidas, postres). Configurables por negocio,
    ver app/api/v1/admin/fixed_menu_items_router.py."""

    fixed_item_id = IDField(include_in_document=True)
    business_id = TextField(required=True)

    nombre = TextField(required=True)
    descripcion = TextField()
    # Uno de PriceType (app.schemas.enum_schemas): fijo, no_legible.
    tipo_precio = TextField(default="fijo")
    precio = NumberField()
    moneda = TextField(default="COP")
    categoria = TextField(default="Fijos")
    is_active = BooleanField(default=True)
    # retailer_id del producto en el catálogo de WhatsApp Business. Lo
    # pone la sincronización (app/services/daily_menu/catalog_sync.py);
    # None = producto creado a mano, que la sincronización nunca toca.
    catalog_retailer_id = TextField()

    created_at = DateTime()
    updated_at = DateTime(auto_update=True)

    class Meta:
        collection_name = "fixed_menu_items"



class CatalogMappings(Model):
    """Qué producto del menú es cada código del catálogo de WhatsApp, cuando
    la sincronización no lo pudo decir (Kapso no deja leer el catálogo). Lo
    confirma el admin una vez (ver app/services/daily_menu/catalog_orders.py)."""

    # sha256(business_id:catalog_id:retailer_id): un negocio nunca ve el de otro.
    catalog_mapping_id = IDField(include_in_document=True)
    business_id = TextField(required=True)
    catalog_id = TextField()
    product_retailer_id = TextField()
    # "fixed:<id>" | "menu_del_dia" | "otras_opciones"; None si sigue pendiente o es ignored.
    producto = TextField()
    status = TextField(default="pending")  # pending | confirmed | ignored
    last_seen_price = NumberField()
    currency = TextField()
    # Conversaciones con un carrito esperando este código.
    esperando = ListField()
    first_seen_at = DateTime()
    confirmed_at = DateTime()
    confirmed_by = TextField()

    class Meta:
        collection_name = "catalog_mappings"


class DailyStock(Model):
    """Un documento por negocio y día (id "<business_id>_<YYYY-MM-DD>"): lo
    que el admin confirmó que queda de cada producto. Sin entrada = sin
    límite. Ver app/services/daily_menu/stock.py."""

    stock_id = IDField(include_in_document=True)
    business_id = TextField()
    fecha = TextField()
    items = MapField()  # {"pollo": {"nombre": "Pollo", "quedan": 3}}

    class Meta:
        collection_name = "daily_stock"


class Messages(Model):

    message_id = IDField(include_in_document=True)
    conversation_id = TextField()
    business_id = TextField()
    customer_id = TextField()

    whatsapp_message_id = TextField()

    role = TextField()

    # --- Handoff bot <-> humano ---
    # direction: uno de MessageDirection (entrante/saliente).
    # origin: uno de MessageOrigin (cliente/ia/humano) -- distingue una
    # respuesta saliente de la IA de un echo saliente de un humano desde la
    # app móvil, algo que `role` (user/assistant, pensado para el runner de
    # ADK) no puede expresar. Campos aditivos, ver Conversations arriba.
    direction = TextField()
    origin = TextField()

    type = TextField()
    content = TextField()

    # Solo para mensajes de media entrante (image/audio/document/video/sticker):
    # el media_id lo entrega WhatsApp en el payload del webhook y sirve para
    # pedir la url temporal de descarga vía WhatsAppClient.download_media().
    media_id = TextField()
    mime_type = TextField()

    status = TextField()

    timestamp = DateTime()
    created_at = DateTime()

    error = TextField()


    class Meta:
        collection_name = "messages"  # subcolección: usar parent=conversation.key


class Orders(Model):
    order_id = IDField(include_in_document=True)
    business_id = TextField()
    conversation_id = TextField()
    # Agregado para que el admin pueda buscar pedidos por cliente
    # (find_orders_by_customer en admin_tools.py) sin tener que pasar por
    # Conversations -> Customers. Los pedidos guardados ANTES de este campo
    # (incluyendo los datos sintéticos de prueba) no lo tienen poblado.
    customer_id = TextField()
    menu_id = TextField()
    # Nombre del producto al momento del pedido. Los reportes lo usan para
    # los extras ("fixed:..."), cuyo menu_id no es legible y cuyo producto
    # puede cambiar o desaparecer del catálogo después.
    item_name = TextField()

    status = TextField()

    quantity = NumberField()
    unit_price = NumberField()
    total = NumberField()

    delivery_address = TextField()
    notes = TextField()

    # Datos del PEDIDO completo (no del ítem): solo los lleva el documento
    # de referencia, el primer ítem -- su order_id es el que devuelve
    # save_order y usan set_payment_method/print_order_ticket.
    # order_total es el total confirmado con el cliente (incluye domicilio);
    # contra él se valida una transferencia, sin recalcular desde el menú.
    order_total = NumberField()
    payment_method = TextField()  # PaymentMethod
    payment_status = TextField()  # PaymentStatus
    # Valor que se leyó en el comprobante de transferencia (auditoría).
    transfer_amount = NumberField()
    # Número de referencia/aprobación leído del comprobante (normalizado),
    # para encontrarlo si hay un reclamo. Ver TransferReceiptHandler.
    transfer_reference = TextField()
    # URLs (Cloud Storage) de TODAS las imágenes que el cliente mandó como
    # comprobante de este pedido, en orden. La imagen no vive en Firestore.
    transfer_receipt_urls = ListField()
    # Transferencia de más: la diferencia, y cuánto de ella es propina según
    # el cliente (None = no ha contestado; 0 = pidió que se la devuelvan).
    transfer_excess = NumberField()
    tip_amount = NumberField()
    # Pago mixto: la transferencia no alcanzó y el cliente confirmó que paga
    # el resto en efectivo al recibir. Sale en la comanda para cobrarlo.
    cash_due = NumberField()
    # Aclaración del pago para quien revise el pedido. Ej. pago mixto: el
    # método queda "transferencia" y acá dice que el resto fue en efectivo.
    payment_note = TextField()
    # Lo que dijo el cliente al pedir un acuerdo especial de pago (ej. "tengo
    # tiquetera"), para que la administradora decida si se cobra.
    payment_agreement = TextField()
    # Domicilio del pedido: zona del catálogo (None si fue cotizado a mano)
    # y tarifa cobrada, ya incluida en order_total.
    delivery_zone = TextField()
    delivery_fee = NumberField()

    # --- Entrega y liquidación (solo el documento de referencia) ---
    # Ver app/services/delivery/tracking.py. `status` es un OrderStatus.
    # Consecutivo corto del día por negocio: lo que se imprime grande en la
    # comanda y lo que escribe el domiciliario al liquidar.
    daily_code = NumberField()
    # Día del pedido 'YYYY-MM-DD' en la zona horaria del negocio.
    service_date = TextField()
    # Domiciliario al que se atribuyó (teléfono normalizado).
    courier_phone = TextField()
    # Otros domiciliarios que también lo liquidaron (conflicto para el cierre).
    conflict_courier_phones = ListField()
    # Incidencia de entrega abierta de esta comanda (solo una a la vez).
    active_incident_id = TextField()

    created_at = DateTime()
    confirmed_at = DateTime()
    dispatched_at = DateTime()
    # Cuándo quedó en entregado_confirmado.
    delivered_at = DateTime()

    updated_at = DateTime()
    class Meta:
        collection_name = "orders"  # subcolección: usar parent=business.key


class OrderStatusHistory(Model):

    order_status_id = IDField(include_in_document=True)

    order_id = TextField()
    business_id = TextField()

    # Historial de eventos de entrega del pedido (ver app/services/delivery/tracking.py).
    # event_type: "liquidado", "conflicto", "presunto_entregado", "reclamo",
    # "no_liquidado" o la señal del cliente ("ya_llego", "comprobante", "queja_contenido").
    event_type = TextField()
    previous_status = TextField()
    new_status = TextField()

    source = TextField()  # DeliveryEventSource
    # Teléfono de quien lo causó (domiciliario/cliente) o un rol ("sistema").
    actor = TextField()
    notes = TextField()

    created_at = DateTime()

    class Meta:
        collection_name = "status_history"  # subcolección: usar parent=order.key


class DailyOrderCounters(Model):
    """Un documento por negocio y día (id "<business_id>_<YYYY-MM-DD>"): el
    consecutivo corto de las comandas y el índice código -> order_id, que
    sirve para liquidar y para el cierre sin consultas con índices nuevos."""

    counter_id = IDField(include_in_document=True)
    business_id = TextField()
    date = TextField()
    last = NumberField()
    orders = MapField()  # {"12": "<order_id>"}
    # Cierre diario ya hecho (lo marca una sola vez el job de domicilios).
    closed_at = DateTime()

    class Meta:
        collection_name = "daily_order_counters"


class Incidents(Model):
    """Problema de un domiciliario con una entrega (no abren, dirección
    errada...). Ver app/services/delivery/incidents.py. Cada transición deja
    un evento en el status_history del pedido."""

    incident_id = IDField(include_in_document=True)
    business_id = TextField()
    order_id = TextField()
    daily_code = NumberField()
    service_date = TextField()
    courier_phone = TextField()
    courier_customer_id = TextField()
    customer_id = TextField()
    customer_phone = TextField()
    motivo = TextField()
    status = TextField()  # IncidentStatus
    # Mensajes enviados al cliente; el temporizador de Cloud Tasks lleva el
    # mismo número y no hace nada si ya no coincide.
    attempts = NumberField()

    created_at = DateTime()
    updated_at = DateTime()
    resolved_at = DateTime()
    escalated_at = DateTime()
    closed_at = DateTime()

    class Meta:
        collection_name = "incidents"


class PrintJobs(Model):

    print_job_id = IDField(include_in_document = True)
    business_id = TextField()
    order_id = TextField()

    content = TextField()

    status = TextField()

    attempts = NumberField()

    created_at = DateTime()
    sent_at = DateTime()
    printed_at = DateTime()
    failed_at = DateTime()

    error = TextField()
    # Cuándo se mandó esta comanda al chat de administración porque no se
    # pudo imprimir (ver print_jobs.send_print_job). None = no hizo falta.
    admin_backup_sent_at = DateTime()


    class Meta:
        collection_name = "print_jobs"  # subcolección: usar parent=business.key


class Devices(Model):
    """Estado ACTUAL de una Raspberry (último heartbeat). Un documento por
    dispositivo, se sobrescribe en cada heartbeat: no es historial.
    Id = "{business_id}--{device_id}", así un device_id repetido en otro
    negocio no pisa este documento."""

    device_doc_id = IDField(include_in_document=True)
    business_id = TextField()
    device_id = TextField()

    # Hora del SERVIDOR al recibir el heartbeat: el estado se calcula con
    # esta, no con el reloj de la Raspberry.
    last_seen_at = DateTime()
    measured_at = DateTime()

    version = TextField()
    commit = TextField()
    agent = TextField()
    printer_usb = BooleanField()
    wifi_ssid = TextField()
    uptime_seconds = NumberField()

    last_print_ok_at = DateTime()
    last_error = TextField()
    last_error_at = DateTime()

    class Meta:
        collection_name = "devices"


class DeviceEvents(Model):
    """Historial de una Raspberry: un documento por CAMBIO detectado entre dos
    heartbeats (no uno por heartbeat). Solo se agrega, nunca se edita."""

    device_event_id = IDField(include_in_document=True)
    business_id = TextField()
    device_id = TextField()
    event_type = TextField()  # DeviceEventType
    # Cuándo empezó (hora del servidor). En "sin_conexion", `until` es cuándo volvió.
    at = DateTime()
    until = DateTime()
    detail = TextField()

    class Meta:
        collection_name = "device_events"


class WebhookEvents(Model):
    webhook_event_id = IDField(include_in_document=True)

    business_id = TextField()

    event_id = TextField()

    provider = TextField()
    event_type = TextField()
    # Uno de WebhookEventKind (message/status/echo/history/app_state_sync) --
    # ver WhatsAppWebhookEventRouter. Aditivo: eventos guardados antes de
    # esto quedan con event_kind=None.
    event_kind = TextField()

    status = TextField()

    attempts = NumberField()

    raw_payload = MapField()

    error = TextField()

    received_at = DateTime()
    processed_at = DateTime()


    class Meta:
        collection_name = "webhook_events"  # subcolección: usar parent=business.key



class Configurations(Model):
    configuration_id = TextField()
    business_id = TextField(required=True)
    
    # --- INFO GENERAL ---
    restaurant_name = TextField()
    manager_phone = TextField()
    # Cómo se nombra a la encargada en los avisos de handoff (ej. "Doña
    # Diana"). Sin valor: "la administradora".
    manager_name = TextField()

    # --- ADMINISTRADORES ---
    # Teléfonos (mismo formato que Customers.phone_number / whatsapp `from`)
    # autorizados a hablar con el admin_agent. El backend resuelve el rol
    # comparando contra esta lista; la IA nunca decide quién es admin.
    admin_phone_numbers = ListField(default=[])

    # --- HANDOFF ---
    # Mensaje que se le manda al cliente cuando el bot se pausa (pedido
    # explícito, precio/dato faltante, baja confianza, o respuesta manual
    # detectada). Si está vacío, se usa
    # HandoffSettings.HANDOFF_DEFAULT_TRANSFER_MESSAGE.
    handoff_transfer_message = TextField()

    # --- MENÚ DEL DÍA ---
    # Hora en texto (ej. "09:00") que la IA de clientes puede dar cuando
    # todavía no hay menú activo hoy. None = no se ofrece hora, solo se dice
    # que el menú no está listo.
    daily_menu_expected_ready_time = TextField()
    # "HH:MM" (DEFAULT_TZ) en que se le recuerda al admin que no ha mandado el
    # menú de hoy. None = no se le recuerda (ej. negocio que solo usa carta).
    # Ver app/services/daily_menu/reminders.py.
    recordatorio_menu_hora = TextField()
    # Lo que dice el recordatorio (va en el {{1}} de la plantilla
    # j2ys_recordatorio_admin). None = DEFAULT_REMINDER_TEXT de reminders.py.
    recordatorio_menu_texto = TextField()
    # OBSOLETO: no se lee (usar admin_templates.recordatorio_menu; ningún
    # negocio lo tenía al quitarlo, 2026-10-20). Se borra del modelo después.
    daily_menu_reminder_template = TextField()

    # --- PLANTILLAS PARA AVISOS AL ADMIN FUERA DE 24H ---
    # {"comanda_respaldo" | "cotizacion_domicilio" | "atencion_cliente" |
    #  "recordatorio_menu": {"name": "<plantilla aprobada>", "language": "es"}}
    # Sin la plantilla de un aviso, ese aviso fuera de 24h no sale y queda
    # un ERROR admin_notice_undelivered (alerta). Ver docs/RUNBOOK.md.
    admin_templates = MapField()

    # --- DOMICILIOS ---
    # Zonas con tarifa fija: [{"nombre": "Cerro Azul", "tarifa": 3000,
    # "alias": ["cerro azul", ...]}]. Una dirección que no cae en ninguna la
    # cotiza un humano (ver app/services/delivery/).
    delivery_zones = ListField(default=[])
    # Domiciliarios del negocio: [{"phone": "573001112233", "name": "Carlos",
    # "added_at": "<iso>"}]. Teléfono normalizado (app/services/phones.py).
    # Se editan desde el chat admin o con PUT .../couriers.
    couriers = ListField(default=[])

    # --- CATÁLOGO DE WHATSAPP ---
    # ID del catálogo de Commerce Manager conectado al número. Vacío = el
    # negocio no usa catálogo (la tool sync_catalog del admin no hace nada).
    whatsapp_catalog_id = TextField()
    # media_id (subido a WhatsApp) de la foto de la CARTA fija. La del menú
    # del día vive en DailyMenus.menu_media_id (ver current_menu_image).
    # Meta lo guarda ~30 días; vencido, el bot muestra el menú en texto.
    carta_media_id = TextField()
    # False = no mandar la imagen del menú sola en el primer turno del día
    # (solo cuando el cliente la pida). None (docs viejos) cuenta como True.
    enviar_menu_al_iniciar = BooleanField(default=True)

    # --- Reglas que usa la IA de clientes (ver app/services/tenant_profile.py) ---
    tono = TextField()
    # Subconjunto de ["efectivo", "transferencia"]; vacío = las dos.
    metodos_pago = ListField(default=[])
    # Cuentas a las que el cliente transfiere: [{"banco", "tipo"
    # (ahorros|corriente|billetera), "numero", "titular", "llave"?}]. Salen
    # tal cual en la respuesta de confirm_order (la IA no las arma).
    cuentas_transferencia = ListField(default=[])
    # Subtotal mínimo (sin domicilio) para confirmar un pedido. None = sin mínimo.
    pedido_minimo = NumberField()
    # [{"pregunta": "...", "respuesta": "..."}]
    faq = ListField(default=[])

    # --- HORARIO ---
    # {timezone, weekly{mon..sun:[{open,close}]}, exceptions[], enforce}; ver
    # app/services/opening_hours.py. Vacío = sin restricción de horario.
    opening_hours = MapField()

    # --- HABILIDADES (FLAGS) ---
    # OBSOLETO: no se lee ni se escribe, ni sale en la API. Se borra del
    # modelo después (fireo carga igual los documentos que lo tengan).
    has_thermal_printer = BooleanField(default=False)
    # True = la comanda no va a la impresora: se manda completa al chat de
    # administración (ver print_jobs.send_print_job).
    orders_to_admin_chat = BooleanField(default=False)
    # OBSOLETO: no se lee ni sale en la API (lo reemplaza la liquidación de
    # domiciliarios). Se borra del modelo después.
    delivery_confirmation_enabled = BooleanField(default=False)

    # --- CONFIGURACIÓN DE IMPRESORA ---
    # Solo se escribe con PUT /api/v1/admin/configurations/{id}/printer
    # (el PATCH general lo ignora). Lo lee print_jobs._printer_for:
    # {
    #   "enabled": True,
    #   "endpoint_url": "https://impresora-del-negocio.example/print",
    #   "auth_secret_ref": "projects/<p>/secrets/printer-token--<businessId>",
    #   "paper_width": "80mm",       # "58mm" | "80mm" | None
    #   "copies": 1,
    #   "timeout_seconds": 15,
    # }
    # El token vive en Secret Manager; acá solo su referencia.
    printer_settings = MapField()

    # --- TIEMPOS Y ALERTAS ---
    # OBSOLETOS: no se leen ni salen en la API (los reemplazan los dos
    # campos de abajo). Se borran del modelo después.
    delivery_confirmation_interval_minutes = NumberField(default=30)
    manager_notification_delay_minutes = NumberField(default=10)
    # Minutos desde la confirmación tras los que un pedido sin reclamo pasa a
    # presunto_entregado. None = nunca se presume.
    tiempo_entrega_estimado_min = NumberField()
    # "HH:MM" (zona horaria del negocio): cierre diario de domicilios. None =
    # sin cierre. Ver app/services/delivery/tracking.py.
    hora_cierre_domicilios = TextField()
    # Incidencias de entrega (ver app/services/delivery/incidents.py): minutos
    # que se espera al cliente entre mensajes y cuántos mensajes extra se le
    # mandan antes de escalar al admin. None = 4 min y 1 reintento.
    incidencia_espera_min = NumberField()
    incidencia_reintentos = NumberField()

    updated_at = DateTime(auto_update=True)
    
    class Meta:
        collection_name = "configurations"

