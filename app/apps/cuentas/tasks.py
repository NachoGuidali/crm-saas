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


@shared_task(name='cuentas.enviar_recordatorios')
def enviar_recordatorios():
    """Manda los recordatorios personales que corresponden a hoy (fecha - días de anticipación)."""
    from core.tenancy import tiene_funcion
    if not tiene_funcion('postventa'):
        return 0
    from .models import RecordatorioPersonal
    from . import services
    hoy = timezone.localdate()
    pendientes = (RecordatorioPersonal.objects
                  .filter(estado=RecordatorioPersonal.ESTADO_PENDIENTE, enviar_el__lte=hoy)
                  .select_related('usuario', 'linea', 'cuenta', 'proyecto')[:500])
    n = 0
    for rec in pendientes:
        services.disparar_recordatorio(rec, hoy)
        n += 1
    return n
