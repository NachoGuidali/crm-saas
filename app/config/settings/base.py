import os
from pathlib import Path

from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env(key, default=''):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    return env(key, str(default)).strip().lower() in ('1', 'true', 'yes', 'si')


SECRET_KEY = env('DJANGO_SECRET_KEY', 'dev-insecure-change-me')
DEBUG = env_bool('DJANGO_DEBUG', True)
ALLOWED_HOSTS = env('ALLOWED_HOSTS', 'localhost 127.0.0.1').split()
CSRF_TRUSTED_ORIGINS = [o.strip() for o in env('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()]

# Nombre visible del sistema (logo, títulos, emails)
CRM_NOMBRE = env('CRM_NOMBRE', 'CRM Ventas')
CRM_SUBTITULO = env('CRM_SUBTITULO', 'SupReg Solutions')

# ── Multi-cliente (SaaS) ───────────────────────────────────────────────────
# Cada cliente tiene su propio schema de Postgres con todas las tablas del CRM (TENANT_APPS) y entra por su
# subdominio (acme.SAAS_DOMINIO). El schema "public" guarda solo lo de la plataforma: clientes, dominios,
# planes y los superusuarios del panel (SHARED_APPS).
SAAS_DOMINIO = env('SAAS_DOMINIO', 'localhost')
# Cobro de la plataforma (ver docs/SAAS.md → Cobros)
SAAS_MONEDA = env('SAAS_MONEDA', 'ARS')
SAAS_DIAS_AVISO = int(env('SAAS_DIAS_AVISO', '5'))        # aviso "por vencer" N días antes
SAAS_DIAS_GRACIA = int(env('SAAS_DIAS_GRACIA', '5'))      # días después del vencimiento antes de suspender
SAAS_SUSPENDER_VENCIDOS = env_bool('SAAS_SUSPENDER_VENCIDOS', True)
SAAS_EMAIL_AVISOS = env_bool('SAAS_EMAIL_AVISOS', True)   # email al cliente antes de vencer y al suspender
SAAS_DATOS_PAGO = env('SAAS_DATOS_PAGO', '')              # ej: "Alias: mi.alias.mp · CBU 000... · Titular X"
SAAS_EMAIL_AVISOS_COPIA = env('SAAS_EMAIL_AVISOS_COPIA', '')  # copia de los avisos (tu email)
MP_ACCESS_TOKEN = env('MP_ACCESS_TOKEN', '')
MP_WEBHOOK_SECRET = env('MP_WEBHOOK_SECRET', '')

SHARED_APPS = [
    'django_tenants',
    'apps.clientes',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'django.contrib.postgres',
    'core',
    'apps.users',  # en público: los superusuarios del panel
]
TENANT_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'apps.users',
    'apps.crm',
    'apps.whatsapp',
    'apps.telefonia',
    'apps.automatizaciones',
    'apps.integraciones',
    'apps.pautas',
    'apps.reportes',
]
INSTALLED_APPS = SHARED_APPS + [a for a in TENANT_APPS if a not in SHARED_APPS]

TENANT_MODEL = 'clientes.Cliente'
TENANT_DOMAIN_MODEL = 'clientes.Dominio'
PUBLIC_SCHEMA_URLCONF = 'config.urls_public'
DATABASE_ROUTERS = ('django_tenants.routers.TenantSyncRouter',)
# Un subdominio que no es de ningún cliente da 404 (no muestra el panel).
SHOW_PUBLIC_IF_NO_TENANT_FOUND = False
# Importante: NO definir SESSION_COOKIE_DOMAIN. Las cookies quedan atadas a cada subdominio, así una sesión de
# un cliente nunca viaja al CRM de otro.

MIDDLEWARE = [
    'django_tenants.middleware.main.TenantMainMiddleware',  # elige el schema según el subdominio
    'apps.clientes.middleware.ClienteSuspendidoMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'apps.users.middleware.ForzarCambioPasswordMiddleware',
    'core.marco.MarcoTelefonoMiddleware',
]
X_FRAME_OPTIONS = 'SAMEORIGIN'  # el marco del teléfono carga las pantallas del CRM en un iframe propio

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.crm',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django_tenants.postgresql_backend',
        'NAME': env('POSTGRES_DB', 'crm_ventas'),
        'USER': env('POSTGRES_USER', 'crm_ventas'),
        'PASSWORD': env('POSTGRES_PASSWORD', 'crm_ventas'),
        'HOST': env('POSTGRES_HOST', 'db'),
        'PORT': env('POSTGRES_PORT', '5432'),
        # Conexiones persistentes: evita abrir una conexión por request con muchos usuarios.
        'CONN_MAX_AGE': int(env('DB_CONN_MAX_AGE', '60')),
        'CONN_HEALTH_CHECKS': True,
    }
}

AUTH_USER_MODEL = 'users.User'
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
AUTHENTICATION_BACKENDS = ['apps.users.backends.UsernameOrEmailBackend']

LOGIN_URL = '/usuarios/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/usuarios/login/'

# Links de recupero de contraseña válidos por 2 horas
PASSWORD_RESET_TIMEOUT = int(env('PASSWORD_RESET_TIMEOUT', str(60 * 60 * 2)))
# Bloqueo de login tras N intentos fallidos (por usuario + IP)
# Cuántos proxies de confianza hay delante (nginx = 1). La IP del cliente se toma de esa posición en
# X-Forwarded-For, para que un atacante no la pueda falsear mandando el header.
PROXIES_CONFIABLES = int(env('PROXIES_CONFIABLES', '1'))
# Vigencia de la firma de los adjuntos salientes de WhatsApp (el proveedor los baja al enviar).
MEDIA_FIRMA_SEGUNDOS = int(env('MEDIA_FIRMA_SEGUNDOS', str(60 * 60)))
LOGIN_MAX_INTENTOS = int(env('LOGIN_MAX_INTENTOS', '5'))
LOGIN_BLOQUEO_MINUTOS = int(env('LOGIN_BLOQUEO_MINUTOS', '15'))

LANGUAGE_CODE = 'es-ar'
TIME_ZONE = 'America/Argentina/Buenos_Aires'
USE_I18N = True
USE_TZ = True
# Sin separador automático: los IDs van en botones, formularios y JS ("10.000" rompía llamar, seleccionar, etc.).
# Para mostrar cantidades grandes: filtros |miles (crm_extras) o |intcomma.
USE_THOUSAND_SEPARATOR = False

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
STORAGES = {
    # Cada cliente guarda sus archivos en media/<schema>/ (ver core.storage).
    'default': {'BACKEND': 'core.storage.ArchivosPorCliente'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# URL pública del dominio principal (panel). La de cada cliente sale de acá cambiando el host por su subdominio
# (core.tenancy.url_sitio): links en emails, webhooks y media enviada desde tareas Celery.
SITE_URL = env('SITE_URL', 'http://localhost:8000').rstrip('/')

# ── Redis / Celery ─────────────────────────────────────────────────────────
REDIS_URL = env('REDIS_URL', 'redis://redis:6379/0')

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': REDIS_URL,
        'KEY_PREFIX': 'crmv',
        # La clave lleva el schema del cliente: contadores, bloqueos y presencia no se cruzan entre clientes.
        'KEY_FUNCTION': 'django_tenants.cache.make_key',
        'REVERSE_KEY_FUNCTION': 'django_tenants.cache.reverse_key',
    }
}
SESSION_ENGINE = 'django.contrib.sessions.backends.cached_db'
SESSION_COOKIE_AGE = 60 * 60 * 12

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = None
CELERY_TASK_IGNORE_RESULT = True
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 60 * 30
# Cada tarea periódica se encola una vez por cliente con acceso (ver core.beat).
CELERY_BEAT_SCHEDULER = 'core.beat:SchedulerPorCliente'
# Colas separadas: los envíos masivos/importaciones no traban los webhooks entrantes.
CELERY_TASK_ROUTES = {
    'whatsapp.procesar_mensaje_entrante': {'queue': 'entrantes'},
    'telefonia.procesar_evento_llamada': {'queue': 'entrantes'},
    'crm.procesar_importacion': {'queue': 'masivos'},
    'crm.accion_masiva': {'queue': 'masivos'},
    'whatsapp.enviar_mensaje': {'queue': 'salientes'},
    'automatizaciones.ejecutar_accion': {'queue': 'salientes'},
    'automatizaciones.difusiones_tick': {'queue': 'masivos'},
}
CELERY_TASK_DEFAULT_QUEUE = 'default'
# El worker cachea el cliente de cada schema (evita una consulta al schema público por tarea).
CELERY_TASK_TENANT_CACHE_SECONDS = 30
CELERY_BEAT_SCHEDULE = {
    'asignar-pendientes': {'task': 'crm.asignar_pendientes', 'schedule': 120},
    'revisar-sla': {'task': 'crm.revisar_sla', 'schedule': 120},
    'revisar-vencimientos': {'task': 'crm.revisar_vencimientos', 'schedule': 900},
    'recalcular-puntajes': {'task': 'crm.recalcular_puntajes', 'schedule': crontab(minute=40)},
    'reactivar-pausadas': {'task': 'crm.reactivar_pausadas', 'schedule': 300},
    'vencer-tareas': {'task': 'crm.notificar_tareas_vencidas', 'schedule': 600},
    'automatizaciones-programadas': {'task': 'automatizaciones.barrer_programadas', 'schedule': 60},
    'difusiones': {'task': 'automatizaciones.difusiones_tick', 'schedule': 60},
    'automatizaciones-sin-actividad': {'task': 'automatizaciones.revisar_sin_actividad', 'schedule': 300},
    'recordatorio-inactividad': {'task': 'automatizaciones.revisar_inactividad', 'schedule': crontab(minute=15)},
    'discador-tick': {'task': 'telefonia.discador_tick', 'schedule': 10},
    'anura-polling-cdrs': {'task': 'telefonia.polling_cdrs', 'schedule': 300},
    'anura-limpiar-llamadas-colgadas': {'task': 'telefonia.cerrar_llamadas_colgadas', 'schedule': 120},
    'whatsapp-estado-lineas': {'task': 'whatsapp.actualizar_estado_lineas', 'schedule': 300},
    'whatsapp-sync-plantillas': {'task': 'whatsapp.sincronizar_plantillas', 'schedule': crontab(minute=0)},
    'purgar-logs': {'task': 'core.purgar_logs', 'schedule': crontab(hour=3, minute=30)},
    # Solo en el schema público (plataforma): avisos de vencimiento y suspensión por falta de pago.
    'clientes-cobros': {'task': 'clientes.revisar_cobros', 'schedule': crontab(hour=9, minute=0),
                        'tenant_schemas': ['public']},
}

# ── Email (recupero de contraseña y mensajes automáticos) ─────────────────
EMAIL_BACKEND = env('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = env('EMAIL_HOST', '')
EMAIL_PORT = int(env('EMAIL_PORT', '587'))
EMAIL_HOST_USER = env('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', True)
DEFAULT_FROM_EMAIL = env('DEFAULT_FROM_EMAIL', 'CRM Ventas <no-responder@localhost>')

# ── WhatsApp: valores default para líneas Evolution (cada línea puede pisarlos) ─
EVOLUTION_API_URL = env('EVOLUTION_API_URL', 'http://evolution-api:8080')
EVOLUTION_API_KEY = env('EVOLUTION_API_KEY', '')

LOG_RETENTION_DAYS = int(env('LOG_RETENTION_DAYS', '90'))
# Supervisión: "Ausente" si tiene el CRM abierto pero no lo toca hace N minutos (no afecta el reparto de leads)
# Landing de prueba pública en /demo/formulario/ (crea leads con origen "Landing Meta"). Apagada salvo DEMO_LANDING=1
DEMO_LANDING = env_bool('DEMO_LANDING', False)
PRESENCIA_AUSENTE_MINUTOS = int(env('PRESENCIA_AUSENTE_MINUTOS', '15'))

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {'verbose': {'format': '{levelname} {asctime} {name} {message}', 'style': '{'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'}},
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'apps': {'handlers': ['console'], 'level': env('APPS_LOG_LEVEL', 'INFO'), 'propagate': False},
    },
}
