import os

from tenant_schemas_celery.app import CeleryApp

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')

# CeleryApp de tenant-schemas-celery: cada tarea viaja con el schema del cliente que la encoló y el worker
# la corre dentro de ese schema (sin esto, una tarea de un cliente leería las tablas del schema público).
app = CeleryApp('crm_ventas')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks(['core', 'apps.clientes', 'apps.crm', 'apps.whatsapp', 'apps.telefonia', 'apps.automatizaciones'])
