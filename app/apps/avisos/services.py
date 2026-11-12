"""
Disparo de avisos internos. Los puntos del CRM donde pasa algo llaman a `disparar(evento, ...)`; acá se buscan
las reglas activas de ese evento, se arman los destinatarios y se avisa por cada canal (campanita al instante,
email y WhatsApp en segundo plano).
"""
import logging
import re

from django.db import transaction
from django.utils import timezone

from .models import EV_EVENTO_EXTERNO, TEXTOS, ReglaAviso

logger = logging.getLogger('apps.avisos')


def disparar(evento, oportunidad=None, conv=None, motivo='', linea=None, evento_nombre=''):
    """Nunca rompe el flujo que lo llama. Devuelve cuántas reglas aplicaron."""
    try:
        return _disparar(evento, oportunidad, conv, motivo, linea, evento_nombre)
    except Exception:
        logger.exception('Aviso %s falló', evento)
        return 0


def _disparar(evento, op, conv, motivo, linea, evento_nombre):
    if op is None and conv is not None and conv.contacto_id:
        from apps.crm.services import oportunidad_activa_de
        op = oportunidad_activa_de(conv.contacto)
    reglas = ReglaAviso.objects.filter(activa=True, evento=evento).prefetch_related('usuarios').select_related('linea')
    n = 0
    for r in reglas:
        if r.embudo_id and (op is None or op.embudo_id != r.embudo_id):
            continue
        if evento == EV_EVENTO_EXTERNO and r.evento_nombre and r.evento_nombre != evento_nombre:
            continue
        aplicar(r, op=op, conv=conv, motivo=motivo, linea=linea)
        n += 1
    return n


def valores(op=None, conv=None, motivo='', linea=None):
    from core import tenancy
    base = tenancy.url_sitio()
    c = (op.contacto if op is not None else None) or (conv.contacto if conv is not None and conv.contacto_id else None)
    if conv is not None:
        link = f'{base}/whatsapp/?conv={conv.pk}'
    elif op is not None:
        link = f'{base}{op.get_absolute_url()}'
    else:
        link = f'{base}/whatsapp/lineas/'
    vendedor = (op.agente if op is not None and op.agente_id else None) or (conv.agente if conv is not None and conv.agente_id else None)
    return {
        'nombre': (c.nombre if c else (conv.nombre_mostrar if conv else '')),
        'telefono': (c.telefono if c else (conv.telefono if conv else '')),
        'motivo': motivo or '', 'etapa': str(op.etapa) if op is not None else '',
        'embudo': str(op.embudo) if op is not None else '',
        'vendedor': vendedor.display_name if vendedor else 'sin asignar',
        'linea': str(linea or (conv.linea if conv is not None else '')), 'link': link,
        'link_rel': link[len(base):] or '/',
    }


def render(texto, vals):
    return re.sub(r'\{(\w+)\}', lambda m: str(vals.get(m.group(1), m.group(0))), texto).strip()


def destinatarios(regla, op=None, conv=None):
    from apps.users.services import supervisores_de
    usuarios = []
    vendedor = (op.agente if op is not None and op.agente_id else None) or (conv.agente if conv is not None and conv.agente_id else None)
    if regla.al_vendedor and vendedor:
        usuarios.append(vendedor)
    if regla.a_supervisores or (regla.al_vendedor and vendedor is None):
        usuarios += supervisores_de(op.embudo if op is not None else None)  # sin vendedor: que se entere alguien
    usuarios += list(regla.usuarios.filter(is_active=True))
    unicos = list({u.pk: u for u in usuarios if u and u.is_active}.values())
    return unicos


def _partir(texto):
    return [x.strip() for x in re.split(r'[,;\n]', texto or '') if x.strip()]


def en_horario(regla, ahora=None):
    if not (regla.desde and regla.hasta):
        return True
    t = timezone.localtime(ahora).time()
    if regla.desde <= regla.hasta:
        return regla.desde <= t <= regla.hasta
    return t >= regla.desde or t <= regla.hasta  # rango que cruza la medianoche (ej. 20 a 8)


def aplicar(regla, op=None, conv=None, motivo='', linea=None):
    from apps.users.services import notificar
    from .tasks import enviar_aviso
    vals = valores(op, conv, motivo, linea)
    texto = render(regla.texto_efectivo, vals)
    titulo = texto.split('\n')[0][:200]
    usuarios = destinatarios(regla, op, conv)
    if regla.por_campanita:
        for u in usuarios:
            notificar(u, 'sistema', titulo, motivo[:150], vals['link_rel'])
    if not en_horario(regla):
        return
    envios = []
    if regla.por_email:
        emails = [u.email for u in usuarios if u.email] + _partir(regla.emails_extra)
        envios += [('email', e) for e in dict.fromkeys(emails)]
    if regla.por_whatsapp and regla.linea_id:
        numeros = [u.whatsapp for u in usuarios if getattr(u, 'whatsapp', '')] + _partir(regla.whatsapps_extra)
        envios += [('whatsapp', n) for n in dict.fromkeys(numeros)]
    regla_pk, evento = regla.pk, regla.evento
    for canal, destino in envios:
        transaction.on_commit(lambda c=canal, d=destino: enviar_aviso.delay(regla_pk, evento, c, d, titulo, texto))


def entregar_directo(usuarios, titulo, texto, email=False, whatsapp_linea=None):
    """Para las automatizaciones "Notificar…": mismo envío por email / WhatsApp, sin regla."""
    from .tasks import enviar_aviso
    for u in usuarios:
        if email and u.email:
            transaction.on_commit(lambda d=u.email: enviar_aviso.delay(None, 'automatizacion', 'email', d, titulo, texto))
        if whatsapp_linea is not None and getattr(u, 'whatsapp', ''):
            transaction.on_commit(lambda d=u.whatsapp, l=whatsapp_linea.pk: enviar_aviso.delay(
                None, 'automatizacion', 'whatsapp', d, titulo, texto, l))


# ── Chats sin atender (lo revisa beat cada 5 minutos) ──────────────────────

def revisar_sin_atender(ahora=None):
    from datetime import timedelta
    from django.core.cache import cache
    from apps.whatsapp.models import Conversacion
    from .models import EV_SIN_ATENDER
    ahora = ahora or timezone.now()
    n = 0
    for r in ReglaAviso.objects.filter(activa=True, evento=EV_SIN_ATENDER):
        limite = ahora - timedelta(minutes=max(r.minutos, 1))
        convs = (Conversacion.objects.filter(estado=Conversacion.ESTADO_PENDIENTE, no_leidos__gt=0, bot_activo=False,
                                             archivada=False, ultimo_entrante_at__lte=limite,
                                             ultimo_entrante_at__gte=ahora - timedelta(days=1))
                 .select_related('contacto', 'agente', 'linea')[:100])
        for conv in convs:
            clave = f'aviso_sa_{r.pk}_{conv.pk}_{int(conv.ultimo_entrante_at.timestamp())}'
            if not cache.add(clave, 1, 60 * 60 * 48):
                continue
            from apps.crm.services import oportunidad_activa_de
            op = oportunidad_activa_de(conv.contacto) if conv.contacto_id else None
            if r.embudo_id and (op is None or op.embudo_id != r.embudo_id):
                continue
            minutos = int((ahora - conv.ultimo_entrante_at).total_seconds() // 60)
            aplicar(r, op=op, conv=conv, motivo=f'{minutos} min')
            n += 1
    return n


def texto_por_defecto(evento):
    return TEXTOS.get(evento, '')
