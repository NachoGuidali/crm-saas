import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger('apps.core')


@shared_task(name='core.purgar_logs')
def purgar_logs():
    """Borra logs técnicos viejos (API de WhatsApp, webhooks, ejecuciones) para que la DB no crezca sin límite."""
    from apps.whatsapp.models import LogAPIWhatsApp
    from apps.integraciones.models import LogIntegracion
    from apps.automatizaciones.models import EjecucionAccion
    from apps.users.models import NotificacionInterna

    corte = timezone.now() - timedelta(days=settings.LOG_RETENTION_DAYS)
    resultado = {
        'log_whatsapp': LogAPIWhatsApp.objects.filter(created_at__lt=corte).delete()[0],
        'log_integraciones': LogIntegracion.objects.filter(created_at__lt=corte).delete()[0],
        'ejecuciones': EjecucionAccion.objects.filter(created_at__lt=corte).delete()[0],
        'notificaciones_leidas': NotificacionInterna.objects.filter(leida=True, created_at__lt=corte).delete()[0],
    }
    logger.info('Purga de logs: %s', resultado)
    return resultado


@shared_task(name='core.latido')
def latido(cola):
    """Lo encola beat cada minuto en cada cola: el worker que lo procesa anota que está vivo (ver core.salud)."""
    from core.salud import latir
    latir(cola)
