import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger('apps.cuentas')


@shared_task(name='cuentas.enviar_avisos_recurrentes')
def enviar_avisos_recurrentes():
    """Manda los avisos de WhatsApp recurrentes que vencen hoy y agenda el próximo. Uno por cliente (beat)."""
    from core.tenancy import tiene_funcion
    if not tiene_funcion('postventa'):
        return 0
    from .models import AvisoRecurrente
    from . import services
    hoy = timezone.localdate()
    pendientes = (AvisoRecurrente.objects.filter(activo=True, proximo_envio__lte=hoy)
                  .select_related('cuenta', 'contacto', 'linea', 'plantilla')[:500])
    n = 0
    for aviso in pendientes:
        services.disparar_aviso(aviso, hoy)
        n += 1
    return n
