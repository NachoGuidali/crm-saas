import logging
from datetime import timedelta

from celery import shared_task
from django.core.files.base import ContentFile
from django.utils import timezone

logger = logging.getLogger('apps.telefonia')


@shared_task(name='telefonia.procesar_evento_llamada', bind=True, max_retries=5, default_retry_delay=5)
def procesar_evento_llamada_task(self, payload, origen='webhook'):
    from .services import procesar_evento_llamada
    try:
        procesar_evento_llamada(payload, origen=origen)
    except Exception as exc:
        logger.exception('Error procesando evento de Anura: %s', exc)
        raise self.retry(exc=exc)


@shared_task(name='telefonia.descargar_grabacion', bind=True, max_retries=6)
def descargar_grabacion(self, llamada_id):
    """La grabación tarda unos segundos en estar disponible tras cortar: se reintenta con espera creciente."""
    from .client import ErrorAnura, get_cliente
    from .models import Llamada
    llamada = Llamada.objects.filter(pk=llamada_id).first()
    if llamada is None or llamada.grabacion:
        return
    cliente = get_cliente()
    try:
        # El evento END trae el link de descarga ({{ audio_file_mp3 }}); si no, se pide a la API de tenant.
        url = (llamada.payload or {}).get('recordingUrl') or (llamada.payload or {}).get('audio_file_mp3')
        if not url:
            if not llamada.call_id:
                raise ErrorAnura('Sin link de grabación ni callId')
            url = cliente.url_grabacion(llamada.call_id)
        if not url:
            raise ErrorAnura('Grabación todavía no disponible')
        contenido = cliente.descargar(url)
    except ErrorAnura as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1))
        Llamada.objects.filter(pk=llamada_id).update(grabacion_estado=Llamada.GRAB_ERROR)
        return
    llamada.grabacion.save(f'{llamada.call_id or llamada.pk}.mp3', ContentFile(contenido), save=False)
    llamada.grabacion_estado = Llamada.GRAB_OK
    llamada.save(update_fields=['grabacion', 'grabacion_estado'])


@shared_task(name='telefonia.polling_cdrs')
def polling_cdrs():
    """Respaldo del webhook: trae los CDRs recientes y los pasa por el mismo procesador (idempotente)."""
    from .client import ErrorAnura, get_cliente
    from .models import ConfigAnura
    from .services import procesar_evento_llamada
    config = ConfigAnura.get()
    if not config.operativa or config.modo_demo or not config.polling_activo or not config.api_url:
        return 0
    hasta = timezone.now()
    desde = (config.ultimo_polling_ok or hasta - timedelta(hours=1)) - timedelta(minutes=5)  # solapamiento
    try:
        cdrs = get_cliente(config).cdrs(desde, hasta)
    except ErrorAnura as e:
        logger.warning('Polling de CDRs falló: %s', e)
        return 0
    n = 0
    for cdr in cdrs:
        try:
            procesar_evento_llamada(cdr, origen='polling', forzar_final=True)
            n += 1
        except Exception as e:  # un CDR malo no frena al resto
            logger.error('CDR no procesado: %s — %s', e, str(cdr)[:200])
    ConfigAnura.objects.filter(pk=1).update(ultimo_polling_ok=hasta)
    from django.core.cache import cache
    cache.delete('config_anura')
    return n


@shared_task(name='telefonia.discador_tick')
def discador_tick():
    from .services import discador_tick as tick
    return tick()


@shared_task(name='telefonia.cerrar_llamadas_colgadas')
def cerrar_llamadas_colgadas():
    from .services import cerrar_llamadas_colgadas as cerrar
    return cerrar()


@shared_task(name='telefonia.evento_demo')
def evento_demo(call_id, tipo):
    """Modo demo: simula los eventos que mandaría el CallHook de Anura."""
    import random
    from .models import Llamada
    from .services import procesar_evento_llamada
    llamada = Llamada.objects.filter(call_id=call_id).first() or Llamada.objects.filter(anura_uuid=call_id).first()
    if llamada is None or not llamada.viva:
        return
    base = {'callId': call_id, 'uuid': call_id, 'direction': llamada.direccion, 'calling': llamada.numero,
            'called': llamada.numero, 'terminal': llamada.interno}
    if tipo == 'ANSWER_START':
        if random.random() < 0.25:  # a veces no atienden, como en la vida real
            procesar_evento_llamada({**base, 'status': 'NOANSWER', 'event': 'END', 'billSeconds': 0}, origen='demo')
        else:
            procesar_evento_llamada({**base, 'status': 'ANSWER', 'event': 'ANSWER'}, origen='demo')
    elif tipo == 'END':
        segundos = int((timezone.now() - (llamada.atendida_at or llamada.inicio_at)).total_seconds())
        status = 'ANSWER' if llamada.atendida_at else 'NOANSWER'
        procesar_evento_llamada({**base, 'status': status, 'event': 'END',
                                 'billSeconds': segundos if llamada.atendida_at else 0}, origen='demo')
