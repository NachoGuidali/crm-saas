import logging

from celery import shared_task

logger = logging.getLogger('apps.avisos')


@shared_task(name='avisos.enviar', bind=True, max_retries=3, default_retry_delay=60)
def enviar_aviso(self, regla_pk, evento, canal, destino, titulo, texto, linea_pk=None):
    from .models import EnvioAviso, ReglaAviso
    error = ''
    try:
        if canal == 'email':
            from django.core.mail import send_mail
            send_mail(titulo, texto, None, [destino], fail_silently=False)
        else:
            regla = ReglaAviso.objects.select_related('linea').filter(pk=regla_pk).first() if regla_pk else None
            from apps.whatsapp.models import LineaWhatsApp
            linea = regla.linea if regla else LineaWhatsApp.objects.filter(pk=linea_pk).first()
            if linea is None:
                raise ValueError('No hay línea elegida para los avisos por WhatsApp.')
            enviar_whatsapp(linea, destino, texto)
    except Exception as e:
        error = str(e)[:300]
        if self.request.retries < self.max_retries and canal == 'email':
            raise self.retry(exc=e)
    EnvioAviso.objects.create(regla_id=regla_pk, evento=evento, canal=canal, destino=destino[:200], texto=texto,
                              ok=not error, error=error)
    return error or 'ok'


def enviar_whatsapp(linea, numero, texto):
    """Manda el aviso por la línea elegida. El chat con el número interno queda archivado y sin bot."""
    from apps.whatsapp import services as wa
    from apps.whatsapp.models import Conversacion
    conv, _ = wa.obtener_conversacion(linea, numero)
    Conversacion.objects.filter(pk=conv.pk).update(bot_activo=False)
    conv.bot_activo = False
    wa.enviar_mensaje(conv, texto=texto, automatico=True)
    Conversacion.objects.filter(pk=conv.pk).update(archivada=True, no_leidos=0)


@shared_task(name='avisos.sin_atender')
def sin_atender():
    """Cada 5 min (en cada cliente): chats esperando respuesta más de lo que dice la regla."""
    from .services import revisar_sin_atender
    return revisar_sin_atender()
