import os
import re

from pydantic import field_validator
from pydantic_settings import BaseSettings,SettingsConfigDict


_base_config = SettingsConfigDict(
        env_file = r".env", # cambiar esto a ruta relativa en prod
        env_ignore_empty = True,
        extra= "ignore" #-> representa que si se añaden variables extras que no esten en el esquema se ignoren
        
    )

class FirestoreSettings(BaseSettings):
    PROJECT_ID: str
    DATABASE: str

    model_config = _base_config


class WhatsAppsettings(BaseSettings):
    # Las credenciales de Meta son opcionales: un despliegue que solo usa
    # Kapso no las tiene. Vacías = esa vía queda cerrada (la firma de Meta
    # se rechaza siempre y el envío directo a Graph API falla con 401).
    TEMP_TOKEN_WPP:str = ""
    VERIFY_TOKEN:str = ""
    PHONE_NUMBER_ID:str = ""
    GRAPH_API_VERSION:str = "v26.0"  # WhatsApp Cloud API (Graph API de Meta)
    # App Secret de la app de Meta (distinto del token de acceso), usado para
    # validar la firma X-Hub-Signature-256 de cada webhook entrante.
    APP_SECRET:str = ""

    # --- Kapso (BSP / tech provider intermediario) ---
    # Proveedor por defecto para ENVIAR: "meta" (Graph API directo, como
    # hasta hoy) o "kapso". Un negocio puede sobrescribirlo con
    # Businesses.whatsapp_provider, así se migra número por número.
    WHATSAPP_PROVIDER:str = "meta"
    # API key del proyecto de Kapso. Va en el header X-API-Key (Kapso
    # responde 401 si se manda como Bearer).
    KAPSO_API_KEY:str = ""
    # Proxy de Kapso con la misma forma que Graph API: solo cambia la base.
    KAPSO_API_BASE_URL:str = "https://api.kapso.ai/meta/whatsapp"
    KAPSO_API_VERSION:str = "v24.0"
    # `secret_key` con el que se registró el webhook (kind "meta") del
    # número en Kapso. Valida el header X-Webhook-Signature. Vacío = todo
    # webhook firmado por Kapso se rechaza.
    KAPSO_WEBHOOK_SECRET:str = ""
    # Webhook por tenant (POST /webhook/kapso/{tenantKey}, ver
    # app/services/tenants.py). Base pública del servicio webhook, para
    # armar la URL que se registra en Kapso. Vacía = la URL sale relativa.
    WEBHOOK_PUBLIC_BASE_URL:str = ""
    # Un mensaje que llega más tarde que esto desde que se escribió se
    # descarta (con 200): limita el replay de un request capturado. Los
    # eventos `history` quedan exentos.
    WEBHOOK_MAX_EVENT_AGE_SECONDS:int = 900

    model_config = _base_config


class DailyMenuSettings(BaseSettings):
    """Ingesta del menú del día por imagen desde el chat admin."""

    # Minutos sin respuesta del admin antes de recordarle que hay un menú
    # pendiente de confirmar (una sola vez).
    DAILY_MENU_REMINDER_MINUTES: int = 30
    # Minutos sin respuesta (desde el recordatorio) antes de descartar el
    # borrador pendiente. Debe ser mayor a DAILY_MENU_REMINDER_MINUTES.
    DAILY_MENU_DISCARD_MINUTES: int = 90
    # La hora del recordatorio de "aún no tengo el menú" es por negocio:
    # Configurations.recordatorio_menu_hora.
    # Nombre de la plantilla aprobada en Meta Business Manager para el
    # La plantilla del recordatorio fuera de la ventana de 24h es por
    # negocio: Configurations.admin_templates.recordatorio_menu.
    MAX_MENU_IMAGE_SIZE_MB: int = 10

    model_config = _base_config


class HandoffSettings(BaseSettings):
    """Umbrales y textos del traspaso bot -> humano (coexistencia)."""

    # Minutos sin un mensaje humano (echo) en una conversación pausada antes
    # de que el job de reactivación la reactive automáticamente. Es un
    # respaldo de horizonte largo (pensado para Cloud Scheduler, no corre
    # más seguido que cada varios minutos) -- el mecanismo principal de
    # reactivación es HANDOFF_QUICK_REACTIVATION_SECONDS, más abajo.
    HANDOFF_INACTIVITY_MINUTES: int = 240
    # Segundos de silencio de la encargada (sin echo) antes de que el
    # siguiente mensaje del cliente reactive el bot automáticamente, sin
    # necesidad de que nadie lo autorice a mano desde el chat admin. Se
    # chequea en el momento en que llega ese mensaje (event-driven), no por
    # un job programado -- 30s es demasiado seguido para Cloud Scheduler.
    HANDOFF_QUICK_REACTIVATION_SECONDS: int = 30
    # Mensaje de traspaso por defecto si el negocio no configuró el suyo en
    # Configurations.handoff_transfer_message.
    HANDOFF_DEFAULT_TRANSFER_MESSAGE: str = (
        "En un momento te atiende alguien de nuestro equipo 🙂"
    )
    # Las plantillas para avisar al encargado fuera de la ventana de 24h son
    # por negocio: Configurations.admin_templates (ver handoff_service).

    model_config = _base_config

class RestaurantAISettings(BaseSettings):
    MODEL_RESTAURANT:str
    # Modelo solo del agente de clientes (el de más volumen); vacío = el
    # mismo MODEL_RESTAURANT. Admin, domiciliarios, menú y comprobantes
    # siguen con MODEL_RESTAURANT.
    MODEL_CUSTOMER: str = ""
    # Pool de fallback en orden de prioridad, separado por comas (ver
    # app/ai/core/model_pool.py). Se cambia con la variable de entorno
    # MODEL_POOL; vacío = solo MODEL_RESTAURANT, sin fallback.
    MODEL_POOL: str = (
        "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,gemini-3.5-flash-lite"
    )

    @field_validator("MODEL_POOL", "MODEL_CUSTOMER")
    @classmethod
    def _only_stable_gemini_3_plus(cls, value: str) -> str:
        for model in filter(None, (m.strip() for m in value.split(","))):
            if re.match(r"gemini-[0-2]\.", model) or re.search(r"preview|exp|latest", model):
                raise ValueError(f"MODEL_POOL solo admite Gemini 3+ estables: {model}")
        return value

    @property
    def model_pool(self) -> list[str]:
        return [m.strip() for m in self.MODEL_POOL.split(",") if m.strip()]

    model_config = _base_config

class AdminAPISettings(BaseSettings):
    # Solo el worker la tiene (secreto admin-api-key). Vacía = la API admin
    # rechaza todo (ver api/v1/deps.require_api_key).
    API_KEY: str = ""

    model_config = _base_config


class PrinterSettings(BaseSettings):
    # Cada negocio imprime en su Configurations.printer_settings (ver
    # print_jobs._printer_for). Transición del antiguo PRINT_URL global: solo
    # el negocio LEGACY_PRINT_BUSINESS_ID puede usar LEGACY_PRINT_URL (sin
    # token, con warning). Vacías = nadie la usa. PRINT_URL ya no se lee.
    LEGACY_PRINT_URL: str = ""
    LEGACY_PRINT_BUSINESS_ID: str = ""

    model_config = _base_config


class VertexAISettings(BaseSettings):
    """Credenciales para que google-adk/google-genai use Vertex AI en vez
    de la Gemini Developer API. Con esto no hace falta GOOGLE_API_KEY: se
    autentica con las Application Default Credentials (gcloud auth
    application-default login / service account)."""

    GOOGLE_GENAI_USE_VERTEXAI: bool = True
    GOOGLE_CLOUD_PROJECT: str
    GOOGLE_CLOUD_LOCATION: str = "us-central1"

    model_config = _base_config


class CloudTasksSettings(BaseSettings):
    """Cola que conecta el webhook liviano (app.services.webhook_entry) con
    el worker que procesa el mensaje (app.services.worker_entry).

    En local se apunta al emulador con CLOUD_TASKS_EMULATOR_HOST (ver
    docker-compose.yml)."""

    # Vacío = usa FirestoreSettings.PROJECT_ID (mismo proyecto de GCP).
    CLOUD_TASKS_PROJECT: str = ""
    CLOUD_TASKS_LOCATION: str = "us-central1"
    CLOUD_TASKS_QUEUE: str = "whatsapp-messages"
    # Cola aparte para los webhooks `history` y `smb_app_state_sync` de
    # coexistencia: llegan en ráfaga al conectar un número (hasta 6 meses de
    # chats) y no deben atrasar los mensajes nuevos de la cola principal.
    # Se crea con límites de velocidad bajos (ver README de despliegue).
    CLOUD_TASKS_HISTORY_QUEUE: str = "whatsapp-history"
    # URL del worker: Cloud Run privado en producción; en local, el uvicorn
    # del worker (ver app/services/worker_entry.py).
    WORKER_URL: str = "http://localhost:8081"
    # Cuenta de servicio del token OIDC con el que Cloud Tasks invoca al
    # worker. Vacío = sin OIDC, solo válido contra el emulador local.
    # Admite una lista separada por comas para cambiar de cuenta sin cortes:
    # las tareas se firman con la PRIMERA y el worker acepta cualquiera.
    WORKER_SERVICE_ACCOUNT_EMAIL: str = ""

    @property
    def task_signer_email(self) -> str:
        return self.WORKER_SERVICE_ACCOUNT_EMAIL.split(",")[0].strip()

    @property
    def task_accepted_emails(self) -> set[str]:
        return {e.strip() for e in self.WORKER_SERVICE_ACCOUNT_EMAIL.split(",") if e.strip()}
    # host:puerto del emulador (github.com/aertje/cloud-tasks-emulator).
    # Vacío = Cloud Tasks real de GCP.
    CLOUD_TASKS_EMULATOR_HOST: str = ""
    # Ventana de agrupado del worker: segundos de silencio del cliente antes
    # de que la IA conteste todo lo acumulado de esa conversación. La espera
    # la hace la cola (schedule_time), no la request, así que subirla no
    # cuesta CPU -- solo hace esperar más al cliente.
    #
    # Es deslizante con tope: si sigue escribiendo, el flush se pospone,
    # pero nunca más de inbound_pipeline._MAX_BATCH_WAIT_SECONDS desde su
    # primer mensaje sin responder.
    MESSAGE_FLUSH_DELAY_SECONDS: float = 5.0

    model_config = _base_config


class StorageSettings(BaseSettings):
    """Bucket privado de Cloud Storage donde se guardan los comprobantes de
    transferencia (ver app/services/payments/receipt_storage.py). Vacío =
    no se guardan (local y tests)."""

    RECEIPTS_BUCKET: str = ""

    model_config = _base_config


class SecretsSettings(BaseSettings):
    """Secretos por negocio en Secret Manager (ver app/services/secrets/).
    Nada de esto es sensible: solo dónde buscar y cuánto cachear."""

    # Vacío = FirestoreSettings.PROJECT_ID.
    SECRETS_PROJECT: str = ""
    # Cuánto se guarda en memoria un secreto leído. Rotar = agregar una
    # versión nueva; tarda como máximo esto en notarse (o un 401 de Kapso,
    # que invalida la caché al instante).
    SECRETS_CACHE_SECONDS: int = 300

    model_config = _base_config


ai_restaurant_settings = RestaurantAISettings()
secrets_settings = SecretsSettings()
storage_settings = StorageSettings()
firestore_settings = FirestoreSettings()
whatsapp_settings = WhatsAppsettings()
admin_api_settings = AdminAPISettings()
vertex_ai_settings = VertexAISettings()
printer_settings = PrinterSettings()
handoff_settings = HandoffSettings()
daily_menu_settings = DailyMenuSettings()
cloud_tasks_settings = CloudTasksSettings()

# google-genai (usado por google-adk) lee estas variables directo de
# os.environ al construir su Client, no desde un objeto de config nuestro.
# Hay que exportarlas una vez acá para que el resto de la app no tenga que
# saber de esto.
os.environ.setdefault(
    "GOOGLE_GENAI_USE_VERTEXAI", str(vertex_ai_settings.GOOGLE_GENAI_USE_VERTEXAI).lower()
)
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", vertex_ai_settings.GOOGLE_CLOUD_PROJECT)
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", vertex_ai_settings.GOOGLE_CLOUD_LOCATION)

