import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger('apps.crm')


@shared_task(name='crm.asignar_pendientes')
def asignar_pendientes():
    """Asigna los prospectos que entraron fuera de horario (o sin agentes disponibles) apenas se puede."""
    from .models import Oportunidad
    from .services import asignar_oportunidad
    n = 0
    qs = (Oportunidad.objects.filter(pendiente_asignacion=True, agente__isnull=True, estado=Oportunidad.ESTADO_ABIERTA)
          .select_related('embudo', 'contacto').order_by('created_at')[:1000])
    for op in qs:
        if op.embudo.en_horario() and asignar_oportunidad(op, forzar_horario=True):
            n += 1
    return n


@shared_task(name='crm.reactivar_pausadas')
def reactivar_pausadas():
    from .models import Oportunidad
    from .services import reanudar
    n = 0
    for op in (Oportunidad.objects.filter(estado=Oportunidad.ESTADO_PAUSADA, proximo_contacto_at__lte=timezone.now())
               .select_related('contacto', 'agente', 'embudo')[:1000]):
        if reanudar(op, automatico=True):
            n += 1
    return n


@shared_task(name='crm.notificar_tareas_vencidas')
def notificar_tareas_vencidas():
    from apps.users.services import notificar
    from .models import Tarea
    vencidas = (Tarea.objects.filter(estado=Tarea.ESTADO_PENDIENTE, notificada_vencida=False, vence_at__lt=timezone.now(),
                                     asignado_a__isnull=False).select_related('asignado_a', 'oportunidad')[:1000])
    ids = []
    for t in vencidas:
        url = t.oportunidad.get_absolute_url() if t.oportunidad_id else '/tareas/'
        notificar(t.asignado_a, 'tarea', f'Tarea vencida: {t.titulo}', '', url)
        ids.append(t.pk)
    Tarea.objects.filter(pk__in=ids).update(notificada_vencida=True)
    return len(ids)


@shared_task(name='crm.procesar_importacion', bind=True, max_retries=0)
def procesar_importacion(self, lote_id):
    from .importacion import procesar_lote
    return procesar_lote(lote_id)


@shared_task(name='crm.accion_masiva', bind=True, max_retries=0)
def accion_masiva(self, user_id, accion, ids, datos):
    from apps.users.models import User
    from apps.users.services import notificar
    from . import masivas
    user = User.objects.get(pk=user_id)
    try:
        resultado = masivas.ejecutar(user, accion, ids, datos)
    except Exception as e:
        logger.exception('Acción masiva %s falló', accion)
        notificar(user, 'sistema', f'Falló la acción masiva: {masivas.ACCIONES.get(accion, accion)}', str(e)[:300],
                  '/oportunidades/')
        return None
    detalle = '; '.join(resultado['errores'][:5])
    notificar(user, 'sistema', masivas.resumen(accion, resultado), detalle[:500], '/oportunidades/')
    return {'hechos': resultado['hechos'], 'errores': len(resultado['errores'])}


@shared_task(name='crm.revisar_sla')
def revisar_sla():
    from .services import revisar_sla as revisar
    return revisar()


@shared_task(name='crm.recalcular_puntajes')
def recalcular_puntajes():
    from .puntaje import recalcular_todos
    return recalcular_todos()


@shared_task(name='crm.revisar_vencimientos')
def revisar_vencimientos():
    from .services import revisar_vencimientos as revisar
    return revisar()
