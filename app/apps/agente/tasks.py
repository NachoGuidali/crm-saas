import logging

import requests
from celery import shared_task
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('apps.agente')


def _con_candado(conv_pk, fn):
    clave = f'agente_lock_{conv_pk}'
    if not cache.add(clave, 1, 180):
        return 'ocupado'
    try:
        return fn()
    finally:
        cache.delete(clave)


@shared_task(name='agente.atender', bind=True, max_retries=3, default_retry_delay=30)
def atender(self, conv_pk, mensaje_pk):
    """Responder a los mensajes del lead (se programa con demora para juntar mensajes seguidos)."""
    from apps.whatsapp.models import Conversacion, Mensaje
    from . import motor
    conv = Conversacion.objects.select_related('linea', 'contacto', 'agente').filter(pk=conv_pk).first()
    if conv is None:
        return 'sin chat'
    posteriores = conv.mensajes.filter(pk__gt=mensaje_pk)
    if posteriores.filter(direccion=Mensaje.DIR_ENTRANTE).exists():
        return 'llegó otro mensaje: responde la tarea siguiente'
    if posteriores.filter(direccion=Mensaje.DIR_SALIENTE).exists():
        return 'ya se respondió'
    asegurar_transcripciones(conv)

    def correr():
        try:
            turno = motor.atender(conv)
        except motor.ErrorAgente as e:
            _avisar_error(str(e))
            return f'error: {e}'
        except Exception as exc:
            if _reintentable(exc):
                raise self.retry(exc=exc)
            _avisar_error(str(exc))
            raise
        return 'respondió' if turno else 'no correspondía'
    return _con_candado(conv_pk, correr)


@shared_task(name='agente.atender_modo', bind=True, max_retries=3, default_retry_delay=60)
def atender_modo(self, conv_pk, modo, motivo='', destinatario_difusion=None, adjunto=None):
    """Primer contacto o seguimiento (el agente escribe sin que el lead haya escrito recién)."""
    from apps.whatsapp.models import Conversacion
    from . import motor
    conv = Conversacion.objects.select_related('linea', 'contacto', 'agente').filter(pk=conv_pk).first()
    if conv is None:
        return 'sin chat'

    def correr():
        try:
            turno = motor.atender(conv, modo=modo, motivo=motivo, adjunto=adjunto)
        except motor.ErrorAgente as e:
            _avisar_error(str(e))
            _difusion(destinatario_difusion, None, f'El agente no pudo escribir: {e}')
            return f'error: {e}'
        except Exception as exc:
            if _reintentable(exc):
                raise self.retry(exc=exc)
            _difusion(destinatario_difusion, None, 'El agente no pudo escribir')
            raise
        _difusion(destinatario_difusion, turno, 'El agente no le escribió (chat tomado, contacto bloqueado o sin presupuesto)')
        return 'escribió' if turno else 'no correspondía'
    return _con_candado(conv_pk, correr)


def _difusion(dest_pk, turno, motivo_si_no):
    """Difusión escrita por el agente: vincula el primer mensaje (para entregados / leídos / respondidos)."""
    if not dest_pk:
        return
    from apps.automatizaciones.models import DifusionDestinatario as D
    msg = turno.mensajes.order_by('pk').first() if turno is not None else None
    if msg is not None:
        D.objects.filter(pk=dest_pk).update(mensaje=msg)
    else:
        D.objects.filter(pk=dest_pk).update(estado=D.OMITIDO, detalle=motivo_si_no[:300])


def _reintentable(exc):
    """Errores pasajeros (límite de uso, red, 5xx) de Anthropic u OpenAI: se reintenta la tarea."""
    import anthropic
    import openai
    for sdk in (anthropic, openai):
        if isinstance(exc, (sdk.RateLimitError, sdk.APIConnectionError)):
            return True
        if isinstance(exc, sdk.APIStatusError) and exc.status_code >= 500:
            return True
    return False


def _avisar_error(texto):
    """Un aviso por hora a los admins si el agente no puede funcionar (falta la API key, key inválida…)."""
    if not cache.add('agente_aviso_error', 1, 3600):
        return
    from django.db.models import Q
    from apps.users.models import User
    from apps.users.services import notificar
    for u in User.objects.filter(is_active=True).filter(Q(rol=User.ROL_ADMIN) | Q(is_superuser=True)):
        notificar(u, 'sistema', 'El agente IA no pudo responder', texto[:200], '/agente/registro/')


def asegurar_transcripciones(conv):
    from apps.whatsapp.models import Mensaje
    from .transcripcion import disponible
    if not disponible():
        return
    for m in conv.mensajes.filter(direccion=Mensaje.DIR_ENTRANTE, tipo=Mensaje.TIPO_AUDIO, transcripcion='') \
            .exclude(media_url='').order_by('-pk')[:3]:
        _transcribir(m)


def _transcribir(m):
    from .transcripcion import ErrorTranscripcion, ruta_de_media, transcribir as tr
    ruta = ruta_de_media(m.media_url)
    if not ruta:
        return
    try:
        texto = tr(ruta)
    except ErrorTranscripcion as e:
        logger.warning('No se pudo transcribir el audio %s: %s', m.pk, e)
        texto = ''
    type(m).objects.filter(pk=m.pk).update(transcripcion=texto or '(no se pudo transcribir)')
    m.transcripcion = texto or '(no se pudo transcribir)'


@shared_task(name='agente.transcribir')
def transcribir(mensaje_pk):
    from apps.whatsapp.models import Mensaje
    m = Mensaje.objects.filter(pk=mensaje_pk, transcripcion='').first()
    if m:
        _transcribir(m)


@shared_task(name='agente.seguimientos')
def seguimientos():
    """Cada 5 min (beat la corre en cada cliente): seguimientos que programó el agente y ya vencieron."""
    _seguimientos_cliente()


def _seguimientos_cliente():
    from . import services
    from .models import SeguimientoAgente
    if not services.habilitado():
        return
    from datetime import timedelta
    from . import lineas
    for s in SeguimientoAgente.objects.filter(hecho=False, cuando__lte=timezone.now()).select_related('conversacion__linea')[:50]:
        conv = s.conversacion
        if s.tipo == SeguimientoAgente.TIPO_RESCATE:
            if not conv.bot_activo:
                s.hecho, s.resultado = True, 'El chat ya no lo atiende el agente'
            elif lineas.cupo_libre(conv.linea) == 0:  # la línea nueva llegó al tope de hoy: mañana a la mañana
                manana = timezone.localtime() + timedelta(days=1)
                s.cuando = manana.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(minutes=s.pk % 180)
                s.save(update_fields=['cuando'])
                continue
            else:
                lineas.reservar_inicio(conv.linea)
                s.hecho, s.resultado = True, 'Retomado'
                atender_modo.delay(conv.pk, 'inicio', s.motivo)
            s.save(update_fields=['hecho', 'resultado'])
            continue
        if conv.ultimo_entrante_at and conv.ultimo_entrante_at > s.created_at:
            s.hecho, s.resultado = True, 'El lead respondió antes'
        elif not conv.bot_activo:
            s.hecho, s.resultado = True, 'El chat ya no lo atiende el agente'
        else:
            s.hecho, s.resultado = True, 'Enviado'
            atender_modo.delay(conv.pk, 'seguimiento', s.motivo)
        s.save(update_fields=['hecho', 'resultado'])


@shared_task(name='agente.sincronizar_precios')
def sincronizar_precios():
    """Cada 30 min (en cada cliente): baja la tabla de precios de Google Sheets si está enlazada."""
    _sincronizar_cliente()


def _sincronizar_cliente():
    from . import services
    from .conocimiento import ErrorConocimiento, sincronizar_google
    from .models import ConfigAgente
    if not services.habilitado():
        return
    cfg = ConfigAgente.objects.exclude(precios_url='').first()
    if cfg is None:
        return
    try:
        sincronizar_google(cfg)
    except ErrorConocimiento as e:
        ConfigAgente.objects.filter(pk=cfg.pk).update(precios_error=str(e)[:300])


@shared_task(name='agente.enviar_derivacion', bind=True, max_retries=6, default_retry_delay=120)
def enviar_derivacion(self, conv_pk, resumen, motivo):
    """Manda el lead derivado al CRM externo (webhook)."""
    from apps.crm.models import Actividad
    from apps.whatsapp.models import Conversacion
    from .models import ConfigAgente
    cfg = ConfigAgente.get()
    conv = Conversacion.objects.select_related('contacto', 'linea').filter(pk=conv_pk).first()
    if conv is None or not cfg.derivar_url:
        return 'nada que enviar'
    payload = payload_derivacion(conv, resumen, motivo)
    headers = {'Content-Type': 'application/json'}
    if cfg.derivar_token:
        headers['Authorization'] = f'Bearer {cfg.derivar_token}'
    try:
        resp = requests.post(cfg.derivar_url, json=payload, headers=headers, timeout=20)
        ok = 200 <= resp.status_code < 300
        detalle = f'HTTP {resp.status_code}'
    except requests.RequestException as e:
        ok, detalle = False, str(e)[:200]
    if not ok and self.request.retries < self.max_retries:
        raise self.retry(exc=Exception(detalle))
    if conv.contacto_id:
        from apps.crm.services import oportunidad_activa_de
        Actividad.objects.create(
            contacto=conv.contacto, oportunidad=oportunidad_activa_de(conv.contacto), tipo=Actividad.TIPO_SISTEMA,
            texto=('Lead enviado al CRM del asesor' if ok else 'No se pudo enviar el lead al CRM del asesor') + f' ({detalle}).')
    return detalle


def payload_derivacion(conv, resumen, motivo):
    from core import tenancy
    from apps.integraciones.webhooks import url_evento
    from apps.crm.services import oportunidad_activa_de
    from . import herramientas
    from .models import ConfigAgente
    c = conv.contacto
    op = oportunidad_activa_de(c) if c else None
    campos = {}
    for clave, etiqueta, _, _ in herramientas.campos_disponibles(ConfigAgente.get()):
        if clave == herramientas.CAMPO_OBSERVACION or c is None:
            continue
        v = getattr(c, clave, None) if hasattr(c, clave) else (c.datos_extra or {}).get(clave)
        campos[clave] = {'etiqueta': etiqueta, 'valor': v.isoformat() if hasattr(v, 'isoformat') else v}
    base = tenancy.url_sitio()
    return {
        'evento': 'lead_derivado',
        'fecha': timezone.now().isoformat(),
        'lead': {'nombre': c.nombre if c else conv.nombre_mostrar, 'telefono': conv.telefono,
                 'email': c.email if c else '', 'dni': c.dni if c else ''},
        'campos': campos,
        'resumen': resumen, 'motivo': motivo,
        'origen': {'linea': str(conv.linea), 'campana': str(op.pauta) if op and op.pauta_id else '',
                   'fuente': op.fuente if op else ''},
        # El CRM de los closers avisa acá el resultado: POST {"evento": "venta"} o {"evento": "no_venta"}
        'callback': ({'url': url_evento(op), 'eventos': ['venta', 'no_venta']} if op else None),
        'crm_origen': {'oportunidad_id': op.pk if op else None, 'conversacion_id': conv.pk,
                       'url_chat': f'{base}/whatsapp/?conv={conv.pk}',
                       'url_ficha': f'{base}{op.get_absolute_url()}' if op else ''},
    }
