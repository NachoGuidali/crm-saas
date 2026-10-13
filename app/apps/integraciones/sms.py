"""SMS por Twilio: envío desde la ficha o automatizaciones, y recepción de respuestas (webhook)."""
import logging

import requests

from .models import ConfigSMS

logger = logging.getLogger('apps.integraciones')


class ErrorSMS(Exception):
    pass


def enviar(contacto, texto, usuario=None, oportunidad=None):
    from apps.crm import services as crm
    from apps.crm.models import Actividad
    config = ConfigSMS.get()
    if not config.operativo:
        raise ErrorSMS('El SMS no está configurado (Integraciones → SMS).')
    texto = (texto or '').strip()
    if not texto:
        raise ErrorSMS('El mensaje está vacío.')
    if not contacto.telefono or not contacto.puede_recibir_mensajes:
        raise ErrorSMS('El contacto no tiene un teléfono válido o pidió no ser contactado.')
    datos = {'To': contacto.telefono, 'Body': texto[:1500]}
    datos['MessagingServiceSid' if config.numero.startswith('MG') else 'From'] = config.numero
    try:
        resp = requests.post(f'https://api.twilio.com/2010-04-01/Accounts/{config.account_sid}/Messages.json',
                             data=datos, auth=(config.account_sid, config.auth_token), timeout=20)
    except requests.RequestException as e:
        raise ErrorSMS(f'No se pudo conectar con Twilio: {e}') from e
    if resp.status_code >= 300:
        try:
            detalle = resp.json().get('message', '')
        except ValueError:
            detalle = resp.text[:200]
        raise ErrorSMS(f'Twilio rechazó el SMS: {detalle}')
    op = oportunidad or crm.oportunidad_activa_de(contacto)
    Actividad.objects.create(contacto=contacto, oportunidad=op, tipo=Actividad.TIPO_SMS, usuario=usuario,
                             texto=f'SMS{" automático" if usuario is None else ""} enviado:\n{texto[:500]}',
                             datos={'sid': resp.json().get('sid', ''), 'direccion': 'out'})
    if op is not None and usuario is not None:
        crm.tocar(op)
        crm.marcar_primer_contacto(op)
    return True


def recibir(telefono, texto):
    """SMS entrante (respuesta del cliente): queda en la ficha y dispara las automatizaciones "Cuando responde"."""
    from apps.crm import services as crm
    from apps.crm.models import Actividad
    contacto = crm.buscar_contacto(telefono)
    if contacto is None:
        logger.info('SMS entrante de un número que no está en el CRM: %s', telefono)
        return None
    op = crm.oportunidad_activa_de(contacto)
    act = Actividad.objects.create(contacto=contacto, oportunidad=op, tipo=Actividad.TIPO_SMS,
                                   texto=f'SMS recibido:\n{texto[:1000]}', datos={'direccion': 'in'})
    if op is not None:
        crm.tocar(op)
        from apps.users.services import notificar
        if op.agente:
            notificar(op.agente, 'whatsapp', f'SMS de {contacto.nombre}', texto[:150], op.get_absolute_url())
        from apps.automatizaciones.services import cliente_respondio
        cliente_respondio(op)
    return act
