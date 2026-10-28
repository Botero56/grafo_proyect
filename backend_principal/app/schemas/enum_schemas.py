from enum import Enum


class BussinessVertical(str,Enum):
    RESTAURANT ='restaurant'
    DENTIST = 'dentist'

class BusinessStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


def is_business_active(business) -> bool:
    """Kill switch: un negocio que no está "active" no pasa nada por la IA.
    Sin status (negocios viejos) cuenta como activo, para no apagarlos."""
    return (getattr(business, "status", None) or BusinessStatus.ACTIVE.value) == BusinessStatus.ACTIVE.value


class ConversationStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CLOSED = "closed"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageStatus(str, Enum):
    RECEIVED = "received"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class PaymentMethod(str, Enum):
    EFECTIVO = "efectivo"
    TRANSFERENCIA = "transferencia"
    # Acuerdo especial con la administradora (tiquetera, pago mensual, ya
    # pagado...): solo lo pone request_payment_agreement, nunca el cliente
    # con set_payment_method. Ver app/services/payments/payment_agreement.py.
    ACUERDO = "acuerdo"


class PaymentStatus(str, Enum):
    # Efectivo: se cobra al entregar, no hay nada que validar.
    POR_COBRAR = "por_cobrar"
    # Transferencia: esperando la foto del comprobante.
    PENDIENTE_COMPROBANTE = "pendiente_comprobante"
    # El valor del comprobante no coincide con el total: lo revisa un humano.
    PENDIENTE_VALIDACION = "pendiente_validacion"
    VALIDADO = "validado"
    # Acuerdo especial: esperando que la administradora diga si se cobra.
    PENDIENTE_AUTORIZACION = "pendiente_autorizacion"
    # La administradora confirmó que este pedido no se cobra.
    SIN_COBRO = "sin_cobro"


class DeliveryStatus(str, Enum):
    # La dirección cayó en una zona del catálogo: tarifa fija de esa zona.
    ZONA = "zona"
    # Dirección fuera del catálogo: espera que un humano cotice el domicilio.
    PENDIENTE_COTIZACION = "pendiente_cotizacion"
    # Un humano ya cotizó el domicilio de esa dirección.
    COTIZADO = "cotizado"


class OrderStatus(str, Enum):
    """Orders.status: estado de ENTREGA del pedido (ver app/services/delivery/tracking.py).
    Lo lleva el documento de referencia; los demás ítems quedan en EN_COCINA."""

    EN_COCINA = "en_cocina"
    # Pasó el tiempo estimado de entrega del negocio sin reclamo del cliente.
    PRESUNTO_ENTREGADO = "presunto_entregado"
    CON_RECLAMO = "con_reclamo"
    # Incluido en una liquidación o confirmado por el domiciliario.
    ENTREGADO_CONFIRMADO = "entregado_confirmado"
    # Nadie lo liquidó al cierre del día.
    NO_LIQUIDADO = "no_liquidado"


class DeliveryEventSource(str, Enum):
    """OrderStatusHistory.source: de dónde salió cada evento del pedido, para
    medir qué mecanismo funciona mejor en cada negocio."""

    LIQUIDACION = "liquidacion"
    CONFIRMACION_TIEMPO_REAL = "confirmacion_tiempo_real"
    SENAL_CLIENTE = "senal_cliente"
    RECLAMO_CLIENTE = "reclamo_cliente"
    TIEMPO_ESTIMADO = "tiempo_estimado"
    CIERRE = "cierre"
    # Eventos de una incidencia de entrega (ver app/services/delivery/incidents.py).
    INCIDENCIA = "incidencia"


class IncidentStatus(str, Enum):
    """Incidents.status. Activas: ESPERANDO_CLIENTE y ESCALADA (una sola por
    comanda); las otras son finales."""

    ESPERANDO_CLIENTE = "esperando_cliente"
    RESUELTA = "resuelta"
    ESCALADA = "escalada"
    CERRADA = "cerrada"


class PrintJobStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    PRINTED = "printed"
    FAILED = "failed"
    # Comanda retenida que ya no debe salir (ej. la administradora dijo que
    # el pedido sí se cobra: la IA arma una nueva al elegir el método).
    CANCELLED = "cancelled"


class DeviceStatus(str, Enum):
    """Estado de una Raspberry derivado de su último heartbeat (ver
    app/services/devices/devices.py::device_status). Nunca se guarda: se
    calcula al leer, para no mostrar como conectado algo que ya no lo está."""

    OPERATIVO = "operativo"
    DEGRADADO = "degradado"  # responde, pero la impresora no está o falló el último print
    DESCONECTADO = "desconectado"  # sin heartbeat reciente
    SIN_INFORMACION = "sin_informacion"


class DeviceEventType(str, Enum):
    """Cambios de una Raspberry que quedan en el historial (DeviceEvents)."""

    REGISTRADA = "registrada"  # primer heartbeat
    SIN_CONEXION = "sin_conexion"  # se guarda al volver, con desde/hasta
    REINICIADA = "reiniciada"  # se reinició sin llegar a contar como caída
    IMPRESORA_DESCONECTADA = "impresora_desconectada"
    IMPRESORA_CONECTADA = "impresora_conectada"
    WIFI_CAMBIADO = "wifi_cambiado"
    VERSION_ACTUALIZADA = "version_actualizada"
    ERROR_IMPRESION = "error_impresion"


class HandoffMotivo(str, Enum):
    SOLICITUD_CLIENTE = "solicitud_cliente"
    PRECIO_VARIABLE = "precio_variable"
    SIN_DATOS = "sin_datos"
    BAJA_CONFIANZA = "baja_confianza"
    RESPUESTA_MANUAL = "respuesta_manual"


class MessageDirection(str, Enum):
    ENTRANTE = "entrante"
    SALIENTE = "saliente"


class MessageOrigin(str, Enum):
    CLIENTE = "cliente"
    IA = "ia"
    HUMANO = "humano"
    # Importado del webhook `history` de coexistencia (chats anteriores a la
    # conexión). Nunca pasa por la IA ni cuenta como pendiente de respuesta.
    HISTORIAL = "historial"


class DailyMenuStatus(str, Enum):
    PENDIENTE_CONFIRMACION = "pendiente_confirmacion"
    ACTIVO = "activo"
    REEMPLAZADO = "reemplazado"
    DESCARTADO = "descartado"
    # El admin avisó que ese día el negocio no abre -- no es "sin menú
    # todavía" (menu_available=False a secas), es un estado explícito para
    # que la IA de clientes pueda avisarle a quien pregunte en vez de
    # ofrecer tomar el pedido o pedir un humano por falta de datos. Ver
    # DailyMenuService.mark_closed_today.
    CERRADO = "cerrado"
    # Carta en varias fotos: el borrador junta los productos de cada foto
    # hasta que el admin elige "menú nuevo" o "juntar"; la activa guarda las
    # fotos que se le mandan al cliente (extracted_menu = {"productos",
    # "media_ids"}). Ver orchestrator._add_carta_page.
    CARTA_PENDIENTE = "carta_pendiente"
    CARTA_ACTIVA = "carta_activa"


class PriceType(str, Enum):
    FIJO = "fijo"
    NO_LEGIBLE = "no_legible"


class WebhookEventKind(str, Enum):
    MESSAGE = "message"
    STATUS = "status"
    ECHO = "echo"
    HISTORY = "history"
    APP_STATE_SYNC = "app_state_sync"
    UNKNOWN = "unknown"

class WhatsAppOnboardingStatus(str, Enum):
    """Businesses.whatsapp_onboarding_status (Embedded Signup)."""

    CONNECTED = "connected"
    FAILED = "failed"
