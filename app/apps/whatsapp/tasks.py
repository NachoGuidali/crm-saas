import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger('apps.whatsapp')


@shared_task(name='whatsapp.procesar_mensaje_entrante', bind=True, max_retries=5, default_retry_delay=10)
def procesar_mensaje_entrante_task(self, linea_id, data):
    from .models import LineaWhatsApp
    from .proveedores import MensajeEntrante
    from .services import procesar_mensaje_entrante
    linea = LineaWhatsApp.objects.filter(pk=linea_id).first()
    if linea is None:
        return
    try:
        procesar_mensaje_entrante(linea, MensajeEntrante.from_dict(data))
    except Exception as exc:
        logger.exception('Error procesando mensaje entrante %s: %s', data.get('wa_id'), exc)
        raise self.retry(exc=exc)


@shared_task(name='whatsapp.enviar_mensaje', bind=True, max_retries=4)
def enviar_mensaje_task(self, mensaje_id):
    from .models import Mensaje
    from .proveedores import ErrorProveedor, get_proveedor

    mensaje = Mensaje.objects.select_related('conversacion__linea', 'plantilla').filter(pk=mensaje_id).first()
    if mensaje is None or mensaje.status != Mensaje.STATUS_PENDIENTE or mensaje.wa_id:
        return  # idempotente: ya se envió o se canceló
    conv = mensaje.conversacion
    proveedor = get_proveedor(conv.linea)
    try:
        if mensaje.plantilla_id:
            wa_id = proveedor.enviar_plantilla(conv.telefono, mensaje.plantilla, mensaje.plantilla_valores or [])
        elif mensaje.media_url:
            wa_id = proveedor.enviar_media(conv.telefono, mensaje.media_url, mensaje.media_mime,
                                           mensaje.media_filename, mensaje.contenido)
        else:
            wa_id = proveedor.enviar_texto(conv.telefono, mensaje.contenido)
    except ErrorProveedor as exc:
        if exc.reintentable and self.request.retries < self.max_retries:
            Mensaje.objects.filter(pk=mensaje_id).update(error=f'Reintentando: {exc}'[:500])
            raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))
        Mensaje.objects.filter(pk=mensaje_id).update(status=Mensaje.STATUS_FALLIDO, error=str(exc)[:1000])
        logger.warning('Mensaje %s falló: %s', mensaje_id, exc)
        return
    Mensaje.objects.filter(pk=mensaje_id, status=Mensaje.STATUS_PENDIENTE).update(
        wa_id=wa_id or '', status=Mensaje.STATUS_ENVIADO, error='')
    if mensaje.contenido and conv.contacto_id and mensaje.automatico:
        _registrar_actividad_automatica(mensaje)


def _registrar_actividad_automatica(mensaje):
    from apps.crm.models import Actividad
    from apps.crm.services import oportunidad_activa_de
    contacto = mensaje.conversacion.contacto
    Actividad.objects.create(
        contacto=contacto, oportunidad=oportunidad_activa_de(contacto), tipo=Actividad.TIPO_WHATSAPP,
        texto=f'Mensaje automático enviado por {mensaje.conversacion.linea}:\n{mensaje.contenido[:500]}',
    )


@shared_task(name='whatsapp.actualizar_estado_lineas')
def actualizar_estado_lineas():
    from .models import LineaWhatsApp
    from .proveedores import get_proveedor
    for linea in LineaWhatsApp.objects.filter(activa=True):
        try:
            estado, detalle = get_proveedor(linea).consultar_estado()
        except Exception as e:  # pragma: no cover
            estado, detalle = LineaWhatsApp.ESTADO_ERROR, str(e)[:300]
        LineaWhatsApp.objects.filter(pk=linea.pk).update(estado=estado, estado_detalle=detalle,
                                                         estado_actualizado_at=timezone.now())


@shared_task(name='whatsapp.sincronizar_plantillas')
def sincronizar_plantillas():
    from .models import LineaWhatsApp
    from .proveedores import get_proveedor
    total = 0
    for linea in LineaWhatsApp.objects.filter(activa=True, proveedor=LineaWhatsApp.PROV_META).exclude(meta_waba_id=''):
        try:
            total += get_proveedor(linea).sincronizar_plantillas()
        except Exception as e:
            logger.warning('No se pudieron sincronizar plantillas de %s: %s', linea, e)
    return total
