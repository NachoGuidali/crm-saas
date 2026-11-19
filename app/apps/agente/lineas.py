"""
Varias líneas por QR trabajando a la vez: el link rotativo reparte los leads, el agente inicia chats sin pasarse
del tope (con calentamiento para las nuevas) y, si una línea se cae o la bloquean, entra una de reserva.
"""
import logging
import re

from django.core.cache import cache
from django.db.models import Q
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger('apps.agente')

# Calentamiento: el día 0 arranca con CAL_BASE chats nuevos y suma CAL_PASO por día hasta el tope de la línea.
CAL_BASE = 15
CAL_PASO = 10
CAL_TOPE_SIN_MAX = 200


def lineas_del_agente():
    from apps.whatsapp.models import LineaWhatsApp as L
    return L.objects.filter(activa=True, agente_ia_activo=True, reserva=False)


def utilizables(qs=None):
    """Las que pueden recibir o iniciar chats ahora: conectadas y con número cargado (para wa.me)."""
    from apps.whatsapp.models import LineaWhatsApp as L
    qs = lineas_del_agente() if qs is None else qs.filter(activa=True, reserva=False)
    return [l for l in qs.order_by('orden', 'pk')
            if l.proveedor == L.PROV_DEMO or l.estado == L.ESTADO_CONECTADA]


def numero_wa(linea):
    return re.sub(r'\D', '', linea.telefono or '')


def elegir_para_enlace(enlace):
    """Siguiente línea en ronda (round-robin) entre las del enlace que están conectadas y tienen número."""
    qs = enlace.lineas.all() if enlace.lineas.exists() else None
    candidatas = [l for l in utilizables(qs) if numero_wa(l)]
    if not candidatas:
        return None
    clave = f'agente_rr_{enlace.pk}'
    n = cache.get(clave, 0)
    cache.set(clave, n + 1, None)
    return candidatas[n % len(candidatas)]


def cupo_dia(linea, hoy=None):
    """Cuántos chats nuevos puede iniciar hoy el agente desde esta línea (None = sin tope)."""
    hoy = hoy or timezone.localdate()
    tope = linea.max_salientes_dia or None
    if linea.calentamiento_inicio:
        dias = max((hoy - linea.calentamiento_inicio).days, 0)
        cal = CAL_BASE + CAL_PASO * dias
        if tope:
            tope = min(cal, tope)
        elif cal < CAL_TOPE_SIN_MAX:
            tope = cal  # sin tope propio: el calentamiento termina al llegar a CAL_TOPE_SIN_MAX
    return tope


def _clave_reservas(linea):
    return f'agente_inicios_{linea.pk}_{timezone.localdate():%Y%m%d}'


def reservar_inicio(linea):
    """Ocupa un lugar del cupo de hoy en el momento de encolar (atómico): varias tandas en paralelo no se pasan."""
    clave = _clave_reservas(linea)
    cache.add(clave, 0, 60 * 60 * 48)
    try:
        return cache.incr(clave)
    except ValueError:
        cache.set(clave, 1, 60 * 60 * 48)
        return 1


def iniciados_hoy(linea):
    """Chats que el agente inició hoy desde esta línea (lo reservado o lo ya escrito, lo que sea mayor)."""
    from .models import TurnoAgente
    inicio = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    escritos = TurnoAgente.objects.filter(conversacion__linea=linea, modo=TurnoAgente.MODO_INICIO, simulado=False,
                                          created_at__gte=inicio, error='').count()
    return max(escritos, cache.get(_clave_reservas(linea), 0) or 0)


def elegir_para_iniciar(contacto):
    """
    Línea para que el agente le escriba primero a un contacto: la del chat que ya tiene (si sigue en servicio);
    si no, la que tenga más cupo libre hoy. None si todas llegaron al tope.
    """
    from apps.whatsapp.models import Conversacion
    disponibles = utilizables()
    if not disponibles:
        return None
    conv = (Conversacion.objects.filter(contacto=contacto, linea__in=disponibles)
            .order_by('-ultimo_mensaje_at').select_related('linea').first())
    if conv:
        return conv.linea
    mejor, libre_mejor = None, -1
    for l in disponibles:
        tope = cupo_dia(l)
        libre = 10 ** 6 if tope is None else tope - iniciados_hoy(l)
        if libre > 0 and libre > libre_mejor:
            mejor, libre_mejor = l, libre
    return mejor


def linea_caida(linea):
    """
    Se desconectó una línea del agente: si hay una de reserva conectada, entra en servicio. Avisa a los admins.
    Devuelve la línea que la reemplazó (o None).
    """
    from apps.users.models import User
    from apps.users.services import notificar
    from apps.whatsapp.models import LineaWhatsApp as L
    if not (linea.agente_ia_activo and not linea.reserva):
        return None
    reemplazo = (L.objects.filter(activa=True, reserva=True, estado=L.ESTADO_CONECTADA)
                 .exclude(pk=linea.pk).order_by('orden', 'pk').first())
    if reemplazo:
        L.objects.filter(pk=reemplazo.pk).update(reserva=False, agente_ia_activo=True)
    cuerpo = (f'Entró en servicio la línea de reserva "{reemplazo}".' if reemplazo
              else 'No hay líneas de reserva conectadas: revisá y conectá otra.')
    rescatados = rescatar_chats(linea)
    if rescatados:
        cuerpo += f' El agente va a retomar {rescatados} chat{"s" if rescatados != 1 else ""} desde las otras líneas.'
    for u in User.objects.filter(is_active=True).filter(Q(rol=User.ROL_ADMIN) | Q(is_superuser=True)):
        notificar(u, 'whatsapp', f'Se desconectó la línea "{linea}" (¿bloqueada?)', cuerpo, '/agente/lineas/')
    logger.warning('Línea del agente caída: %s → reemplazo %s', linea, reemplazo)
    return reemplazo


GAP_RESCATE = timedelta(minutes=3)  # entre retomes por línea (además de las pausas anti-ráfaga de la línea)


def rescatar_chats(caida):
    """
    Los chats que el agente atendía en una línea caída pasan a otra línea del agente (vinculados al anterior, así el
    agente tiene todo el historial) y se programa un mensaje para retomar, escalonado y dentro del cupo diario.
    Devuelve cuántos se programaron.
    """
    from apps.crm.models import Actividad
    from apps.crm.services import oportunidad_activa_de
    from apps.whatsapp import services as wa
    from apps.whatsapp.models import Conversacion
    from .models import ConfigAgente, SeguimientoAgente
    cfg = ConfigAgente.get()
    if not cfg.rescatar_chats:
        return 0
    destinos = [l for l in utilizables() if l.pk != caida.pk]
    if not destinos:
        return 0
    ahora = timezone.now()
    convs = (Conversacion.objects.filter(linea=caida, bot_activo=True, contacto__isnull=False,
                                         ultimo_mensaje_at__gte=ahora - timedelta(days=cfg.rescate_dias))
             .exclude(contacto__no_contactar=True).exclude(contacto__telefono_invalido=True)
             .select_related('contacto').order_by('-ultimo_mensaje_at'))
    proximo = {l.pk: ahora + timedelta(minutes=1) for l in destinos}
    n = 0
    for i, conv in enumerate(convs):
        destino = destinos[i % len(destinos)]
        nueva, _ = wa.obtener_conversacion(destino, conv.telefono, conv.nombre_perfil)
        Conversacion.objects.filter(pk=nueva.pk).update(contacto=conv.contacto, continua_de=conv, bot_activo=True,
                                                        bot_derivada=False, agente=conv.agente)
        Conversacion.objects.filter(pk=conv.pk).update(bot_activo=False)
        SeguimientoAgente.objects.filter(conversacion=conv, hecho=False).update(hecho=True, resultado='Línea caída: se retoma desde otra')
        SeguimientoAgente.objects.create(conversacion_id=nueva.pk, cuando=proximo[destino.pk], tipo=SeguimientoAgente.TIPO_RESCATE,
                                         motivo=cfg.mensaje_rescate[:300] or 'Retomá la charla desde el número nuevo.')
        proximo[destino.pk] += GAP_RESCATE
        Actividad.objects.create(contacto=conv.contacto, oportunidad=oportunidad_activa_de(conv.contacto),
                                 tipo=Actividad.TIPO_SISTEMA,
                                 texto=f'Se cayó la línea "{caida}": el agente retoma el chat desde "{destino}".')
        n += 1
    if n:
        wa.marcar_cambio_inbox()
    return n


def cupo_libre(linea):
    tope = cupo_dia(linea)
    return None if tope is None else max(tope - iniciados_hoy(linea), 0)
