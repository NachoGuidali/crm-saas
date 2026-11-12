"""Enganche del agente con el resto del CRM (entrantes de WhatsApp, automatizaciones)."""
import logging

from django.db import transaction

from core.tenancy import tiene_funcion

logger = logging.getLogger('apps.agente')

FUNCION = 'agente_ia'


def habilitado():
    return tiene_funcion(FUNCION)


def al_recibir(conv, mensaje):
    """Llamado al registrar un entrante. Programa la respuesta del agente (y la transcripción de audios)."""
    if not habilitado():
        return False
    from apps.whatsapp.models import Mensaje
    from .transcripcion import disponible
    from . import tasks
    msg_pk, conv_pk = mensaje.pk, conv.pk
    if mensaje.tipo == Mensaje.TIPO_AUDIO and disponible():
        transaction.on_commit(lambda: tasks.transcribir.delay(msg_pk))
    if not (conv.linea.agente_ia_activo and conv.bot_activo):
        return False
    from .models import ConfigAgente
    demora = ConfigAgente.get().demora_segundos
    transaction.on_commit(lambda: tasks.atender.apply_async((conv_pk, msg_pk), countdown=demora))
    return True


def linea_cambio_estado(linea, anterior, nuevo):
    """Una línea del agente que estaba conectada se cayó: entra la de reserva y se avisa."""
    from apps.whatsapp.models import LineaWhatsApp as L
    if anterior == L.ESTADO_CONECTADA and nuevo in (L.ESTADO_DESCONECTADA, L.ESTADO_ERROR):
        from apps.avisos.models import EV_LINEA_CAIDA
        from apps.avisos.services import disparar
        disparar(EV_LINEA_CAIDA, linea=linea, motivo='Puede estar bloqueada o se cerró la sesión: revisá la conexión.')
    if anterior == L.ESTADO_CONECTADA and nuevo in (L.ESTADO_DESCONECTADA, L.ESTADO_ERROR) and habilitado():
        from .lineas import linea_caida
        linea.estado = nuevo
        return linea_caida(linea)
    return None


def iniciar_chat(oportunidad, objetivo=''):
    """
    El agente le escribe primero a un lead (automatización o prospección). Elige la línea con cupo, abre el chat
    con el bot activo y encola el turno. Devuelve (ok, detalle, reintentar): reintentar=True si hoy no hay cupo
    en ninguna línea (se prueba mañana).
    """
    from apps.whatsapp import services as wa
    from apps.whatsapp.models import Conversacion
    from . import lineas, tasks
    from .models import ConfigAgente
    if not habilitado():
        return False, 'El módulo Agente IA no está habilitado.', False
    if not ConfigAgente.get().activo:
        return False, 'El agente está apagado.', False
    contacto = oportunidad.contacto
    if not contacto.puede_recibir_mensajes:
        return False, 'El contacto no acepta mensajes.', False
    linea = lineas.elegir_para_iniciar(contacto)
    if linea is None:
        return False, 'Sin cupo hoy en las líneas del agente: se reintenta mañana.', True
    conv = wa.conversacion_para_contacto(contacto, linea)
    if not conv.bot_activo and conv.mensajes.exists():
        return False, 'Ese chat ya lo atiende una persona.', False
    Conversacion.objects.filter(pk=conv.pk).update(bot_activo=True, bot_derivada=False)
    conv_pk = conv.pk
    transaction.on_commit(lambda: tasks.atender_modo.delay(conv_pk, 'inicio', objetivo))
    return True, f'El agente le escribe desde {linea}.', False
