"""
Celery beat multi-cliente: cada tarea de CELERY_BEAT_SCHEDULE se encola una vez por cliente, dentro de su
schema (asignaciones, SLA, discador, automatizaciones...). Los clientes suspendidos o dados de baja no corren.
Las tareas con 'tenant_schemas': ['public'] corren una sola vez, en el schema de la plataforma.
"""
from tenant_schemas_celery.scheduler import TenantAwarePersistentScheduler


class SchedulerPorCliente(TenantAwarePersistentScheduler):
    @classmethod
    def get_queryset(cls):
        from django.db.models import Q
        from django_tenants.utils import get_public_schema_name

        from apps.clientes.models import Cliente
        # El público se incluye para las tareas de la plataforma ('tenant_schemas': ['public']); a las tareas del
        # CRM ("todos los clientes") la librería ya lo excluye.
        return Cliente.objects.filter(Q(estado__in=Cliente.ESTADOS_CON_ACCESO) | Q(schema_name=get_public_schema_name()))
